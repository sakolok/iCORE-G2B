import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

from fastapi import Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models import UserModel


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    final_salt = salt or secrets.token_hex(16)
    raw = f"{final_salt}:{password}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return final_salt, digest


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, candidate_hash = hash_password(password, salt)
    return hmac.compare_digest(candidate_hash, expected_hash)


def _sign(data: bytes) -> str:
    signature = hmac.new(settings.auth_secret_key.encode("utf-8"), data, hashlib.sha256).hexdigest()
    return signature


def create_access_token(username: str, role: str) -> str:
    payload = {
        "username": username,
        "role": role,
        "iat": int(datetime.now(timezone.utc).timestamp()),
    }
    raw_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(raw_payload).decode("ascii")
    signature = _sign(raw_payload)
    return f"{encoded_payload}.{signature}"


def parse_access_token(token: str) -> dict:
    try:
        encoded_payload, signature = token.split(".", maxsplit=1)
        raw_payload = base64.urlsafe_b64decode(encoded_payload.encode("ascii"))
        expected_signature = _sign(raw_payload)
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("invalid signature")
        return json.loads(raw_payload.decode("utf-8"))
    except Exception as error:
        raise ValueError("invalid token") from error


def authenticate_user(db: Session, username: str, password: str) -> UserModel:
    user = (
        db.execute(select(UserModel).where(UserModel.username == username, UserModel.is_active.is_(True)))
        .scalar_one_or_none()
    )
    if user is None or not verify_password(password, user.password_salt, user.password_hash):
        raise ValueError("아이디 또는 비밀번호가 올바르지 않습니다.")
    return user


def require_auth(authorization: str | None = Header(default=None)) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다.")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer 토큰 형식이 아닙니다.")

    token = authorization.split(" ", maxsplit=1)[1].strip()
    try:
        payload = parse_access_token(token)
        if not payload.get("username"):
            raise ValueError("invalid payload")
        return payload
    except ValueError as error:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.") from error


def verify_scraper_internal_token(
    x_scraper_internal_token: str | None = Header(default=None),
) -> None:
    expected = settings.scraper_internal_token.strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="SCRAPER_INTERNAL_TOKEN이 설정되지 않았습니다.",
        )
    if not x_scraper_internal_token or not hmac.compare_digest(
        x_scraper_internal_token.strip(), expected
    ):
        raise HTTPException(status_code=401, detail="유효하지 않은 내부 토큰입니다.")
