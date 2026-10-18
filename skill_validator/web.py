"""Web interface (Hebrew, RTL). Runs no agent work itself; the worker process does.

    python -m skill_validator.web          # http://127.0.0.1:8000
"""

import difflib
import os
import secrets
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from starlette.middleware.sessions import SessionMiddleware

from . import PROJECT_ROOT
from .auth import consume_login_token, issue_login_token
from .db import (
    AgentRun,
    BudgetApproval,
    FileRecord,
    OutputArtifact,
    ReviewItem,
    ReviewSession,
    ReviewTask,
    User,
    ValidationResult,
)
from .generator import plan_candidate, request_generation
from .orchestrator import (
    Services,
    WorkflowError,
    allowed_actions,
    approve_ceiling,
    approve_proposal,
    budget_sufficient,
    ceilings,
    confirm_purpose,
    current_decision,
    delete_review,
    remaining,
    reset_decision,
    resume,
    set_decision,
    spent,
    start_review,
    undecided_items,
)
from .reports import (
    ACTION_LABELS,
    CERTAINTY_LABELS,
    COVERAGE_LABELS,
    FINAL_LABELS,
    OUTCOME_LABELS,
    RELEVANCE_LABELS,
    SAFETY_LABELS,
    SEVERITY_LABELS,
)

HERE = PROJECT_ROOT / "skill_validator"

STAGE_LABELS = {
    "intake": "קליטה",
    "purpose": "הגדרת מטרה",
    "analyze": "בדיקה",
    "decisions": "בחירה ותיקון",
    "revalidate": "בדיקה חוזרת",
    "delivered": "נמסר",
    "rejected": "נדחה בקליטה",
}
STATUS_LABELS = {
    "running": "בעבודה",
    "waiting_user": "ממתין להחלטה שלך",
    "stopped": "נעצר",
    "done": "הסתיים",
    "rejected": "נדחה",
}
STOP_LABELS = {
    "budget_exceeded": "התקציב של הסקירה נוצל",
    "budget_insufficient": "היתרה אינה מספיקה להמשך",
    "model_error": "שגיאה או סירוב של שירות המודל",
    "total_budget_cap": "התקרה הכוללת להרצות בתשלום נוצלה",
    "source_changed": "המקור השתנה",
}
DOWNLOAD_NAMES = {"package_zip": "חבילה מתוקנת (ZIP)", "review_md": "REVIEW.md", "changes_md": "CHANGES.md"}


def create_app(svc: Services) -> FastAPI:
    app = FastAPI(title="Skill Validator", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("SV_SESSION_SECRET") or secrets.token_urlsafe(32),
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        STAGE_LABELS=STAGE_LABELS, STATUS_LABELS=STATUS_LABELS, STOP_LABELS=STOP_LABELS,
        ACTION_LABELS=ACTION_LABELS, RELEVANCE_LABELS=RELEVANCE_LABELS, SAFETY_LABELS=SAFETY_LABELS,
        SEVERITY_LABELS=SEVERITY_LABELS, CERTAINTY_LABELS=CERTAINTY_LABELS, OUTCOME_LABELS=OUTCOME_LABELS,
        FINAL_LABELS=FINAL_LABELS, COVERAGE_LABELS=COVERAGE_LABELS, DOWNLOAD_NAMES=DOWNLOAD_NAMES,
        mock_mode=svc.agent_cfg.model.provider == "mock",
        config_version=svc.agent_cfg.config_version,
    )
    app.state.svc = svc
    app.state.last_login_link = None  # for local testing only

    def db_dep():
        db = svc.db_factory()
        try:
            yield db
        finally:
            db.close()

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = request.session["csrf"] = secrets.token_urlsafe(16)
        return tok

    def check_csrf(request: Request, token: str):
        if not token or token != request.session.get("csrf"):
            raise HTTPException(400, "טופס לא תקף. רעננו את הדף ונסו שוב.")

    def current_user(request: Request, db=Depends(db_dep)) -> User:
        uid = request.session.get("user_id")
        user = db.get(User, uid) if uid else None
        if user is None:
            raise HTTPException(303, headers={"Location": "/login"})
        return user

    def owned_review(review_id: str, user: User, db) -> ReviewSession:
        review = db.get(ReviewSession, review_id)
        if review is None or review.user_id != user.id:
            raise HTTPException(404, "הסקירה לא נמצאה")
        return review

    def render(request, name, **ctx):
        ctx.setdefault("user", None)
        return templates.TemplateResponse(request, name, {"csrf": csrf_token(request), **ctx})

    def back(review_id: str, error: str | None = None, anchor: str = ""):
        url = f"/reviews/{review_id}" + (f"?error={quote(error)}" if error else "") + anchor
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------ login

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, sent: int = 0):
        return render(request, "login.html", sent=sent)

    @app.post("/login")
    def login_request(request: Request, email: str = Form(...), csrf: str = Form(""), db=Depends(db_dep)):
        check_csrf(request, csrf)
        try:
            token = issue_login_token(db, email)
        except ValueError as e:
            return render(request, "login.html", sent=0, error=str(e))
        link = str(request.base_url).rstrip("/") + f"/login/{token}"
        app.state.last_login_link = link
        print(f"\n[Skill Validator] login link for {email.strip().lower()}: {link}\n", flush=True)
        return RedirectResponse("/login?sent=1", status_code=303)

    @app.get("/login/{token}")
    def login_consume(request: Request, token: str, db=Depends(db_dep)):
        user = consume_login_token(db, token)
        if user is None:
            return render(request, "login.html", sent=0, error="הקישור אינו תקף או שכבר נוצל")
        request.session.clear()
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------ dashboard

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: User = Depends(current_user), db=Depends(db_dep)):
        reviews = db.query(ReviewSession).filter_by(user_id=user.id).order_by(ReviewSession.created_at.desc()).all()
        rows = [{"r": r, "remaining": remaining(r)} for r in reviews]
        return render(request, "dashboard.html", user=user, rows=rows)

    @app.get("/reviews/new", response_class=HTMLResponse)
    def new_review(request: Request, user: User = Depends(current_user)):
        return render(request, "new.html", user=user, cfg=svc.agent_cfg, intake=svc.system_cfg.intake,
                      formats=svc.system_cfg.supported_formats)

    @app.post("/reviews")
    async def create_review(
        request: Request,
        files: list[UploadFile] = File(...),
        usage_description: str = Form(""),
        disclosure: str = Form(""),
        csrf: str = Form(""),
        user: User = Depends(current_user),
        db=Depends(db_dep),
    ):
        check_csrf(request, csrf)
        limit = svc.system_cfg.intake.max_total_bytes + 1024
        uploads, total = [], 0
        for f in files:
            if not f.filename:
                continue
            data = await f.read(limit + 1)
            total += len(data)
            if total > limit:
                return render(request, "new.html", user=user, cfg=svc.agent_cfg, intake=svc.system_cfg.intake,
                              formats=svc.system_cfg.supported_formats, error="ההעלאה גדולה מהמותר")
            uploads.append((f.filename, data))
        try:
            review = start_review(svc, db, user, uploads, usage_description, disclosure == "yes")
        except WorkflowError as e:
            return render(request, "new.html", user=user, cfg=svc.agent_cfg, intake=svc.system_cfg.intake,
                          formats=svc.system_cfg.supported_formats, error=str(e))
        return RedirectResponse(f"/reviews/{review.id}", status_code=303)

    # ------------------------------------------------------------ review page

    @app.get("/reviews/{review_id}", response_class=HTMLResponse)
    def review_page(request: Request, review_id: str, error: str = "", user: User = Depends(current_user), db=Depends(db_dep)):
        review = owned_review(review_id, user, db)
        items = db.query(ReviewItem).filter_by(session_id=review.id).order_by(ReviewItem.sort_key).all()
        validations = {v.item_id: v for v in db.query(ValidationResult).filter_by(session_id=review.id, kind="semantic")}
        cards = []
        for item in items:
            d = current_decision(db, item.id)
            cards.append({
                "item": item,
                "decision": d,
                "actions": sorted(allowed_actions(item, d), key=list(ACTION_LABELS).index),
                "diff": _diff(svc, review, item, d),
                "validation": validations.get(item.id),
                "pending": d is None or d.approval_status != "approved",
            })
        cards.sort(key=lambda c: (not c["pending"], c["item"].sort_key))
        preview, preview_error = None, ""
        if review.stage == "decisions":
            try:
                preview = plan_candidate(svc, db, review)
            except WorkflowError as e:
                preview_error = str(e)
        tasks = db.query(ReviewTask).filter_by(session_id=review.id).all()
        progress = {
            "done": sum(t.status == "done" for t in tasks),
            "total": len(tasks),
            "skipped": sum(t.status == "skipped" for t in tasks),
        }
        files = db.query(FileRecord).filter_by(session_id=review.id, area="source").order_by(FileRecord.relative_path).all()
        artifacts = db.query(OutputArtifact).filter_by(session_id=review.id).all()
        structural = db.query(ValidationResult).filter_by(session_id=review.id, kind="structural").first()
        approvals = db.query(BudgetApproval).filter_by(session_id=review.id).order_by(BudgetApproval.approved_at).all()
        return render(
            request, "review.html", user=user, review=review, cards=cards, undecided=len(undecided_items(db, review)),
            preview=preview, preview_error=preview_error, progress=progress, files=files, artifacts=artifacts,
            structural=structural, approvals=approvals, error=error,
            budget={"spent": spent(review), "ceiling": ceilings(review), "remaining": remaining(review),
                    "sufficient": budget_sufficient(review, svc.agent_cfg),
                    "per_task": svc.agent_cfg.limits.per_task.model_dump(),
                    "max": svc.agent_cfg.limits.max_approved_ceiling.model_dump()},
            auto_refresh=review.status == "running",
        )

    def act(review_id, user, db, fn, anchor=""):
        review = owned_review(review_id, user, db)
        try:
            fn(review)
        except WorkflowError as e:
            db.rollback()
            return back(review_id, str(e), anchor)
        return back(review_id, anchor=anchor)

    def act_then(review_id, user, db, fn, url):
        review = owned_review(review_id, user, db)
        try:
            fn(review)
        except WorkflowError as e:
            db.rollback()
            return back(review_id, str(e))
        return RedirectResponse(url, status_code=303)

    @app.post("/reviews/{review_id}/purpose")
    def post_purpose(request: Request, review_id: str, purpose: str = Form(...), csrf: str = Form(""),
                     user: User = Depends(current_user), db=Depends(db_dep)):
        check_csrf(request, csrf)
        return act(review_id, user, db, lambda r: confirm_purpose(svc, db, r, purpose))

    def _item(db, review, item_id) -> ReviewItem:
        item = db.get(ReviewItem, item_id)
        if item is None or item.session_id != review.id:
            raise HTTPException(404)
        return item

    @app.post("/reviews/{review_id}/items/{item_id}/decision")
    def post_decision(request: Request, review_id: str, item_id: str, action: str = Form(...), csrf: str = Form(""),
                      user: User = Depends(current_user), db=Depends(db_dep)):
        check_csrf(request, csrf)
        return act(review_id, user, db, lambda r: set_decision(db, r, _item(db, r, item_id), action), f"#item-{item_id}")

    @app.post("/reviews/{review_id}/items/{item_id}/approve")
    def post_approve(request: Request, review_id: str, item_id: str, csrf: str = Form(""),
                     user: User = Depends(current_user), db=Depends(db_dep)):
        check_csrf(request, csrf)

        def fn(r):
            d = current_decision(db, _item(db, r, item_id).id)
            if d is None:
                raise WorkflowError("אין החלטה לאשר")
            approve_proposal(db, r, d)

        return act(review_id, user, db, fn, f"#item-{item_id}")

    @app.post("/reviews/{review_id}/items/{item_id}/reset")
    def post_reset(request: Request, review_id: str, item_id: str, csrf: str = Form(""),
                   user: User = Depends(current_user), db=Depends(db_dep)):
        check_csrf(request, csrf)
        return act(review_id, user, db, lambda r: reset_decision(db, r, _item(db, r, item_id)), f"#item-{item_id}")

    @app.post("/reviews/{review_id}/generate")
    def post_generate(request: Request, review_id: str, csrf: str = Form(""),
                      user: User = Depends(current_user), db=Depends(db_dep)):
        check_csrf(request, csrf)
        return act(review_id, user, db, lambda r: request_generation(svc, db, r))

    @app.post("/reviews/{review_id}/resume")
    def post_resume(request: Request, review_id: str, csrf: str = Form(""),
                    user: User = Depends(current_user), db=Depends(db_dep)):
        check_csrf(request, csrf)
        return act(review_id, user, db, lambda r: resume(svc, db, r))

    @app.post("/reviews/{review_id}/ceiling")
    def post_ceiling(request: Request, review_id: str, tokens: int = Form(...), cost_usd: float = Form(...),
                     processing_minutes: float = Form(...), confirm: str = Form(""), csrf: str = Form(""),
                     user: User = Depends(current_user), db=Depends(db_dep)):
        check_csrf(request, csrf)

        def fn(r):
            if confirm != "yes":
                raise WorkflowError("יש לאשר במפורש את הגדלת התקרה")
            approve_ceiling(svc, db, r, user,
                            {"tokens": tokens, "cost_usd": cost_usd, "processing_minutes": processing_minutes})

        return act(review_id, user, db, fn)

    @app.post("/reviews/{review_id}/delete")
    def post_delete(request: Request, review_id: str, csrf: str = Form(""),
                    user: User = Depends(current_user), db=Depends(db_dep)):
        check_csrf(request, csrf)
        return act_then(review_id, user, db, lambda r: delete_review(svc, db, r), "/")

    @app.get("/reviews/{review_id}/download/{kind}")
    def download(review_id: str, kind: str, user: User = Depends(current_user), db=Depends(db_dep)):
        review = owned_review(review_id, user, db)
        if review.stage != "delivered":
            raise HTTPException(404, "התוצרים זמינים רק אחרי הבדיקה החוזרת")
        art = db.query(OutputArtifact).filter_by(session_id=review.id, kind=kind).one_or_none()
        if art is None:
            raise HTTPException(404)
        data = svc.storage.resolve("delivery", user.id, review.id, art.storage_path).read_bytes()
        media = "application/zip" if kind == "package_zip" else "text/markdown; charset=utf-8"
        return Response(data, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{art.storage_path}"'})

    # ------------------------------------------------------------ operations dashboard (O10: metadata only)

    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request, user: User = Depends(current_user), db=Depends(db_dep)):
        if not user.is_admin:
            raise HTTPException(403, "דשבורד התפעול זמין למנהלי מערכת בלבד")
        by_final = dict(db.query(ReviewSession.final_status, func.count()).group_by(ReviewSession.final_status).all())
        by_status = dict(db.query(ReviewSession.status, func.count()).group_by(ReviewSession.status).all())
        by_severity = dict(db.query(ReviewItem.severity, func.count()).group_by(ReviewItem.severity).all())
        by_defect = dict(db.query(ReviewItem.defect_type, func.count()).group_by(ReviewItem.defect_type).all())
        runs = db.query(AgentRun.outcome, func.count()).group_by(AgentRun.outcome).all()
        per_model = db.query(AgentRun.config_version, AgentRun.model_id, func.count(),
                             func.sum(AgentRun.cost_usd), func.sum(AgentRun.input_tokens + AgentRun.output_tokens)
                             ).group_by(AgentRun.config_version, AgentRun.model_id).all()
        n_reviews = db.query(func.count(ReviewSession.id)).scalar() or 0
        avg_cost = db.query(func.avg(ReviewSession.spent_cost_total)).scalar() or 0
        return render(
            request, "ops.html", user=user, n_reviews=n_reviews, by_final=by_final, by_status=by_status,
            by_severity=by_severity, by_defect=by_defect, runs=dict(runs), per_model=per_model, avg_cost=avg_cost,
            approvals=db.query(func.count(BudgetApproval.id)).scalar() or 0,
            skipped=db.query(func.count(ReviewTask.id)).filter(ReviewTask.status == "skipped").scalar() or 0,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return templates.TemplateResponse(
            request, "error.html", {"status": exc.status_code, "detail": exc.detail, "user": None, "csrf": ""},
            status_code=exc.status_code,
        )

    return app


def _diff(svc: Services, review: ReviewSession, item: ReviewItem, d) -> list[tuple[str, str]] | None:
    if d is None or not d.proposed_text:
        return None
    main = item.locations[0]
    src = svc.storage.resolve("source", review.user_id, review.id, main["path"]).read_text(encoding="utf-8").splitlines()
    before = src[main["start"] - 1 : main["end"]]
    lines = []
    for line in difflib.unified_diff(before, d.proposed_text.splitlines(), lineterm="", n=2):
        if line.startswith(("---", "+++", "@@")):
            continue
        kind = "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx"
        lines.append((kind, line))
    return lines


def main() -> None:
    import argparse

    import uvicorn

    from .services import build_services

    parser = argparse.ArgumentParser(description="Skill Validator web interface (local)")
    parser.add_argument("--config", help="agent config path (default: $SV_CONFIG)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-root", help="data folder relative to the project (default: config/system.yaml)")
    args = parser.parse_args()
    app = create_app(build_services(args.config, args.data_root))
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
