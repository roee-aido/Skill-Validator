"""REVIEW.md and CHANGES.md (SPEC section 15). Built from the database only; secrets masked."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .db import FileRecord, ReviewItem, ReviewSession, ValidationResult
from .orchestrator import Services, current_decision
from .secretscan import mask_secrets

ACTION_LABELS = {
    "keep": "להשאיר",
    "keep_open": "להשאיר כנושא פתוח",
    "remove": "להסיר",
    "fix": "נוסח מתוקן",
    "adapt": "התאמה למטרה",
}
RELEVANCE_LABELS = {"relevant": "רלוונטי", "not_relevant": "לא רלוונטי"}
SAFETY_LABELS = {"ok": "תקין במסגרת הבדיקה", "suspicious": "חשוד", "unsafe": "לא בטוח", "unknown": "לא ידוע"}
SEVERITY_LABELS = {"info": "מידע בלבד", "low": "נמוכה", "medium": "בינונית", "high": "גבוהה", "critical": "קריטית"}
CERTAINTY_LABELS = {"low": "נמוכה", "medium": "בינונית", "high": "גבוהה"}
OUTCOME_LABELS = {
    "closed": "נסגר: הסיכון לא נמצא בעותק המתוקן",
    "remained": "נותר: הסיכון עדיין קיים בעותק המתוקן",
    "accepted_by_user": "נשאר לפי בחירת המשתמש",
    "not_applicable": "לא רלוונטי: חלק תקין שנשמר",
}
COVERAGE_LABELS = {"checked": "נבדק", "not_checked": "לא נבדק", "unsupported": "לא נתמך (לא נבדק)", "pending": "ממתין"}
FINAL_LABELS = {
    "completed_no_open": "הבדיקה הושלמה ללא ממצאים פתוחים",
    "completed_with_open": "הבדיקה הושלמה עם ממצאים פתוחים",
    "partial": "הבדיקה חלקית",
}


def _loc(loc: dict) -> str:
    rng = f"{loc['start']}" if loc["start"] == loc["end"] else f"{loc['start']}-{loc['end']}"
    tag = " (הפניה)" if loc.get("role") == "reference" else ""
    return f"`{loc['path']}:{rng}`{tag}"


def _fence(text: str) -> str:
    text = mask_secrets(text or "")
    return "```text\n" + text.replace("```", "``​`") + "\n```"


def _mode_notice(svc: Services) -> list[str]:
    if svc.agent_cfg.model.provider == "mock":
        return [
            "> **מצב מודל מדומה:** הסקירה בוצעה עם מודל מדומה מבוסס כללים (`mock-rule-based`), ללא קריאות API.",
            "> הסיווגים מבוססים על תבניות קבועות ואינם משקפים שיפוט של מודל שפה. העלויות בדוח מדומות.",
        ]
    return []


def build_review_md(svc: Services, db: Session, review: ReviewSession, final: str) -> str:
    items = db.query(ReviewItem).filter_by(session_id=review.id).order_by(ReviewItem.sort_key).all()
    results = db.query(ValidationResult).filter_by(session_id=review.id).all()
    by_item = {r.item_id: r for r in results if r.kind == "semantic"}
    structural = [r for r in results if r.kind == "structural"]
    files = db.query(FileRecord).filter_by(session_id=review.id, area="source").order_by(FileRecord.relative_path).all()

    out = [f"# דוח סקירה: {review.skill_name}", ""]
    out.append(f"> **מצב:** {FINAL_LABELS[final]}. מצב ההשלמה אינו הבטחת בטיחות.")
    out += _mode_notice(svc)
    out += [
        "",
        "## פרטי הסקירה",
        "",
        f"- תאריך הפקה: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- גרסת קונפיגורציה: `{review.config_version}`",
        f"- מודל: `{review.model_id}`",
        f"- hash של המקור: `{review.source_hash}`",
        f"- hash של העותק שנבדק: `{structural[0].candidate_hash if structural else '-'}`",
        f"- הוצאה מצטברת: {review.spent_tokens_total:,} טוקנים, ${review.spent_cost_total:.4f}"
        + (" (מדומה)" if svc.agent_cfg.model.provider == "mock" else ""),
        "",
        "**מטרה מאושרת:** " + (review.purpose or "-"),
        "",
        "## כיסוי הבדיקה",
        "",
        "| קובץ | סוג | מצב |",
        "|---|---|---|",
    ]
    for f in files:
        out.append(f"| `{f.relative_path}` | {f.type} | {COVERAGE_LABELS.get(f.coverage_status, f.coverage_status)} |")

    out += ["", "## ממצאים", ""]
    for n, item in enumerate(items, start=1):
        d = current_decision(db, item.id)
        r = by_item.get(item.id)
        action = ACTION_LABELS.get(d.action, d.action) + (" (ברירת מחדל)" if d.is_default else "") if d else "-"
        out += [
            f"### {n}. {item.title}",
            "",
            f"- מיקום: {', '.join(_loc(l) for l in item.locations)}",
            f"- סיווג: {RELEVANCE_LABELS[item.relevance]}, {SAFETY_LABELS[item.safety_status]}",
            f"- חומרה: {SEVERITY_LABELS[item.severity]} · ודאות: {CERTAINTY_LABELS[item.certainty]} · סוג: `{item.defect_type}`",
            f"- סיבה: {mask_secrets(item.reason)}",
        ]
        if item.dependencies:
            out.append(f"- תלויות: {item.dependencies}")
        out += [f"- החלטת המשתמש: {action}"]
        if r:
            out.append(f"- בדיקה חוזרת: {OUTCOME_LABELS.get(r.outcome, r.outcome)}. {mask_secrets(r.evidence)}")
            for issue in r.new_issues:
                out.append(f"  - בעיה חדשה: {issue}")
        out += ["", "ראיה:", "", _fence(item.evidence), ""]

    out += ["## בדיקה מבנית של העותק המתוקן", ""]
    for r in structural:
        out.append(f"- תוצאה: {'עברה' if r.outcome == 'passed' else 'נמצאו בעיות'}. {r.evidence}")
        for issue in r.new_issues:
            out.append(f"  - {issue}")

    remaining = [i for i in items if by_item.get(i.id) and by_item[i.id].outcome in ("remained", "accepted_by_user")]
    out += ["", "## סיכונים שנותרו", ""]
    out += [f"- {i.title} ({OUTCOME_LABELS[by_item[i.id].outcome]})" for i in remaining] or ["- לא נותרו ממצאים פתוחים מתוך הממצאים שזוהו."]

    out += [
        "",
        "## מגבלות",
        "",
        "- הבדיקה סטטית בלבד: הקוד, הקישורים והוראות ההתקנה לא הורצו ולא נפתחו.",
        "- תלויות שלא סופקו בחבילה לא נבדקו.",
    ]
    if review.unchecked_resources:
        out.append("- קבצים שלא נבדקו: " + ", ".join(f"`{p}`" for p in review.unchecked_resources))
    out.append(
        "- הגבלת הרשת בסביבה המקומית נאכפת בשכבת הגנה בתוך התהליך (audit hook), ולא בבידוד ברמת התשתית."
    )
    out += _mode_notice(svc)
    return "\n".join(out) + "\n"


def build_changes_md(svc: Services, db: Session, review: ReviewSession, plan) -> str:
    out = [f"# פירוט שינויים: {review.skill_name}", ""]
    out += _mode_notice(svc)
    out += ["", "כל שינוי משויך לכרטיס ולהחלטה שאישרה אותו. המקור לא שונה.", ""]
    out += ["## סיכום", "", "| # | חלק | החלטה | מיקום | השפעה על יכולות |", "|---|---|---|---|---|"]
    for n, c in enumerate(plan.changes, start=1):
        loc = _loc({"path": c["path"], "start": c["start"], "end": c["end"], "role": c["role"]})
        out.append(f"| {n} | {c['title']} | {ACTION_LABELS[c['action']]} | {loc} | {c['impact'] or '-'} |")
    if not plan.changes:
        out.append("| - | לא בוצעו שינויים | - | - | - |")

    out += ["", "## פירוט", ""]
    for n, c in enumerate(plan.changes, start=1):
        out += [f"### {n}. {c['title']}", "", f"- החלטה: {ACTION_LABELS[c['action']]}", f"- מזהה כרטיס: `{c['item_id']}`", ""]
        out += ["לפני:", "", _fence(c["before"]), ""]
        if c["after"] is None:
            out += ["אחרי: הקובץ הוסר מהעותק המתוקן.", ""]
        elif c["after"] == "":
            out += ["אחרי: השורות הוסרו.", ""]
        else:
            out += ["אחרי:", "", _fence(c["after"]), ""]

    kept = []
    for item in db.query(ReviewItem).filter_by(session_id=review.id).order_by(ReviewItem.sort_key):
        d = current_decision(db, item.id)
        if d and d.action in ("keep", "keep_open"):
            kept.append(f"- {item.title}: {ACTION_LABELS[d.action]}" + (" (ברירת מחדל)" if d.is_default else ""))
    out += ["## חלקים שנשמרו ללא שינוי", ""] + (kept or ["- אין"])
    out += ["", "## קבצים שלא הועתקו לעותק המתוקן", ""]
    out += [f"- `{p}` (פורמט לא נתמך, לא נבדק)" for p in plan.not_copied] or ["- אין"]
    return "\n".join(out) + "\n"
