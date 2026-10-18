import re

import pytest
from fastapi.testclient import TestClient

from skill_validator.auth import issue_login_token
from skill_validator.db import ReviewItem, ReviewSession
from skill_validator.orchestrator import run_until_idle
from skill_validator.web import create_app

from .conftest import DEMO_ZIP


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, svc, email="owner@example.test"):
    db = svc.db_factory()
    token = issue_login_token(db, email)
    db.close()
    r = client.get(f"/login/{token}", follow_redirects=False)
    assert r.status_code == 303
    return client


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc))


def post(client, url, data=None, files=None, page="/"):
    token = csrf_of(client.get(page).text)
    return client.post(url, data={**(data or {}), "csrf": token}, files=files, follow_redirects=False)


def upload(client, path=DEMO_ZIP, confirm=True):
    data = {"usage_description": "לשכתב טקסט"}
    if confirm:
        data["disclosure"] = "yes"
    return post(client, "/reviews", data, files=[("files", (path.name, path.read_bytes(), "application/zip"))],
                page="/reviews/new")


def review_id(r) -> str:
    return r.headers["location"].rsplit("/", 1)[-1]


def test_pages_require_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "כניסה" in client.get("/login").text


def test_login_link_is_one_time(client, svc):
    db = svc.db_factory()
    token = issue_login_token(db, "a@example.test")
    db.close()
    assert client.get(f"/login/{token}", follow_redirects=False).status_code == 303
    assert "אינו תקף" in client.get(f"/login/{token}").text


def test_csrf_is_required(client, svc):
    login(client, svc)
    r = client.post("/reviews", data={"disclosure": "yes"},
                    files=[("files", (DEMO_ZIP.name, DEMO_ZIP.read_bytes(), "application/zip"))])
    assert r.status_code == 400


def test_full_flow_through_the_web_interface(client, svc):
    login(client, svc)
    page = client.get("/reviews/new").text
    assert "מה נשלח ולאן" in page and "Anthropic" in page

    r = upload(client, confirm=False)
    assert "יש לאשר" in r.text  # disclosure must be confirmed
    r = upload(client)
    rid = review_id(r)
    assert "בעבודה" in client.get(f"/reviews/{rid}").text

    run_until_idle(svc)
    page = client.get(f"/reviews/{rid}").text
    assert "מטרת הסקיל" in page
    post(client, f"/reviews/{rid}/purpose", {"purpose": "שכתוב טקסט כך שיישמע טבעי"}, page=f"/reviews/{rid}")
    run_until_idle(svc)
    page = client.get(f"/reviews/{rid}").text
    assert "חלקים שנמצאו" in page and "גישה רחבה מדי" in page and "disabled" in page

    db = svc.db_factory()
    items = {i.defect_type: i.id for i in db.query(ReviewItem).filter_by(session_id=rid)}
    db.close()
    post(client, f"/reviews/{rid}/items/{items['broad_file_access']}/decision", {"action": "fix"}, page=f"/reviews/{rid}")
    run_until_idle(svc)
    page = client.get(f"/reviews/{rid}").text
    assert "sample texts they choose" in page and 'class="add"' in page
    post(client, f"/reviews/{rid}/items/{items['broad_file_access']}/approve", page=f"/reviews/{rid}")
    for defect in ("concealment", "reviewer_manipulation", "network_send"):
        post(client, f"/reviews/{rid}/items/{items[defect]}/decision", {"action": "remove"}, page=f"/reviews/{rid}")

    # Downloads are refused before delivery.
    assert client.get(f"/reviews/{rid}/download/review_md").status_code == 404

    r = post(client, f"/reviews/{rid}/generate", page=f"/reviews/{rid}")
    assert "error" not in r.headers["location"]
    run_until_idle(svc)
    page = client.get(f"/reviews/{rid}").text
    assert "הבדיקה הושלמה ללא ממצאים פתוחים" in page and "אינו הבטחת בטיחות" in page

    md = client.get(f"/reviews/{rid}/download/review_md")
    assert md.status_code == 200 and "דוח סקירה" in md.text
    pkg = client.get(f"/reviews/{rid}/download/package_zip")
    assert pkg.status_code == 200 and pkg.content[:2] == b"PK"
    assert "דוח" in client.get("/").text or "human-writing-demo" in client.get("/").text


def test_bad_archive_shows_rejection(client, svc):
    login(client, svc)
    rid = review_id(upload(client, DEMO_ZIP.parent / "bad-archive.zip"))
    page = client.get(f"/reviews/{rid}").text
    assert "ההעלאה נדחתה" in page and "נתיב חורג" in page


def test_users_cannot_see_each_others_reviews(svc):
    a = login(TestClient(create_app(svc)), svc, "a@example.test")
    rid = review_id(upload(a))
    b = login(TestClient(create_app(svc)), svc, "b@example.test")
    assert b.get(f"/reviews/{rid}").status_code == 404
    assert b.get(f"/reviews/{rid}/download/package_zip").status_code == 404


def test_budget_approval_through_the_interface(client, svc):
    login(client, svc)
    rid = review_id(upload(client))
    db = svc.db_factory()
    review = db.get(ReviewSession, rid)
    review.status, review.stop_reason = "stopped", "budget_exceeded"
    review.spent_cost_total = review.budget_cost_ceiling - 0.1
    db.commit()
    db.close()
    page = client.get(f"/reviews/{rid}").text
    assert "היתרה אינה מספיקה" in page and "אישור תקרה חדשה" in page

    cap = svc.agent_cfg.limits.max_approved_ceiling
    data = {"tokens": cap.tokens, "cost_usd": 9.0, "processing_minutes": 30}
    r = post(client, f"/reviews/{rid}/ceiling", data, page=f"/reviews/{rid}")  # no explicit confirmation
    assert "error" in r.headers["location"]
    r = post(client, f"/reviews/{rid}/ceiling", {**data, "confirm": "yes"}, page=f"/reviews/{rid}")
    assert "error" not in r.headers["location"]
    db = svc.db_factory()
    review = db.get(ReviewSession, rid)
    assert review.status == "running" and review.budget_cost_ceiling == 9.0
    assert review.spent_cost_total == pytest.approx(5.9)
    db.close()


def test_ops_dashboard_is_admin_only_and_shows_no_content(svc, monkeypatch):
    user = login(TestClient(create_app(svc)), svc, "user@example.test")
    assert user.get("/ops").status_code == 403
    monkeypatch.setenv("SV_ADMIN_EMAILS", "admin@example.test")
    admin = login(TestClient(create_app(svc)), svc, "admin@example.test")
    rid = review_id(upload(admin))
    run_until_idle(svc)
    page = admin.get("/ops").text
    assert "דשבורד תפעול" in page and "mock-rule-based" in page
    assert "Synthetic test skill" not in page and "description:" not in page


def test_delete_review_removes_everything(client, svc):
    login(client, svc)
    rid = review_id(upload(client))
    run_until_idle(svc)
    db = svc.db_factory()
    uid = db.get(ReviewSession, rid).user_id
    db.close()
    post(client, f"/reviews/{rid}/delete", page="/")
    assert client.get(f"/reviews/{rid}").status_code == 404
    assert not svc.storage.area_dir("source", uid, rid).exists()
