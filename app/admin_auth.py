"""Small single-operator administrator session for Hostless deployments.

The password is supplied by ADMIN_PASSWORD in the Hostless environment. The
browser only receives a short-lived, signed, secure, HTTP-only cookie.
"""
import hashlib
import hmac
import re
import secrets
import time

from fastapi import HTTPException, Request

COOKIE_NAME = "dynamic_outro_admin"
COOKIE_PATH = "/api/admin"
SESSION_TTL_SECONDS = 8 * 3600
SESSION_RE = re.compile(r"^(\d{10})\.([a-f0-9]{32})\.([a-f0-9]{64})$")


def _key(password: str) -> bytes:
    # Changing ADMIN_PASSWORD invalidates all existing sessions.
    return hashlib.sha256(b"dynamic-outro-admin-cookie-v1\0" + password.encode()).digest()


def password_matches(supplied: str, configured: str) -> bool:
    if not configured or not isinstance(supplied, str):
        return False
    # Compare hashes so hmac.compare_digest receives equal-length inputs.
    return hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(),
                               hashlib.sha256(configured.encode()).digest())


def new_session(password: str, now: int | None = None) -> str:
    issued = str(int(time.time()) if now is None else int(now))
    nonce = secrets.token_hex(16)
    message = f"{issued}.{nonce}"
    signature = hmac.new(_key(password), message.encode(), hashlib.sha256).hexdigest()
    return f"{message}.{signature}"


def valid_session(token: str | None, password: str, now: int | None = None) -> bool:
    if not password or not token or len(token) > 150:
        return False
    match = SESSION_RE.fullmatch(token)
    if not match:
        return False
    issued = int(match.group(1))
    current = int(time.time()) if now is None else int(now)
    if issued > current + 60 or current - issued > SESSION_TTL_SECONDS:
        return False
    message = f"{match.group(1)}.{match.group(2)}"
    expected = hmac.new(_key(password), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(3))


def require_admin(request: Request, password: str) -> None:
    if not password:
        raise HTTPException(503, "Admin password has not been configured")
    if not valid_session(request.cookies.get(COOKIE_NAME), password):
        raise HTTPException(401, "Administrator login required")


def require_admin_action(request: Request, password: str) -> None:
    require_admin(request, password)
    # Custom header blocks ordinary cross-origin form/CSRF requests; browser
    # cross-origin JavaScript cannot send it without CORS preflight permission.
    if request.headers.get("x-admin-request") != "1":
        raise HTTPException(403, "Invalid administrator request")
