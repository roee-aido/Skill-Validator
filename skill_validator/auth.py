"""One-time login links for local development (D3 with the local default for O2).

The link is printed to the web server's console. There are no passwords. A real
identity provider replaces this before any deployment.
"""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .db import LoginToken, User

TOKEN_TTL = timedelta(minutes=15)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("SV_ADMIN_EMAILS", "").split(",") if e.strip()}


def issue_login_token(db: Session, email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise ValueError("כתובת מייל לא תקינה")
    token = secrets.token_urlsafe(32)
    db.add(LoginToken(token_hash=_hash(token), email=email, expires_at=datetime.now(timezone.utc) + TOKEN_TTL))
    db.commit()
    return token


def consume_login_token(db: Session, token: str) -> User | None:
    row = db.query(LoginToken).filter_by(token_hash=_hash(token)).one_or_none()
    now = datetime.now(timezone.utc)
    if row is None or row.used_at is not None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires < now:
        return None
    row.used_at = now
    user = db.query(User).filter_by(email=row.email).one_or_none()
    if user is None:
        user = User(email=row.email)
        db.add(user)
    user.is_admin = row.email in admin_emails()
    db.commit()
    return user
