import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token as google_id_token
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.database import get_db
from app.data.models import (
    OrganizationMemberModel,
    OrganizationModel,
    UserModel,
)


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


def create_access_token(user_id: int, username: str, role: str) -> str:
    issued_at = datetime.now(timezone.utc)
    payload = {
        "ver": 2,
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(hours=settings.auth_token_ttl_hours)).timestamp()),
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
        payload = json.loads(raw_payload.decode("utf-8"))
        if payload.get("ver") != 2 or not payload.get("sub"):
            raise ValueError("unsupported token")
        if int(payload.get("exp") or 0) <= int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("expired token")
        return payload
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


class GoogleLoginConfigurationError(RuntimeError):
    pass


class GoogleLoginCredentialError(ValueError):
    pass


class GoogleLoginAccessError(PermissionError):
    pass


def verify_google_identity(credential: str) -> dict[str, str]:
    client_id = settings.google_oauth_client_id.strip()
    allowed_domains = {
        domain.strip().lower().lstrip("@")
        for domain in settings.allowed_login_domains
        if domain.strip()
    }
    if not client_id or not allowed_domains:
        raise GoogleLoginConfigurationError(
            "Google 로그인이 아직 설정되지 않았습니다."
        )

    try:
        payload = google_id_token.verify_oauth2_token(
            credential,
            GoogleRequest(),
            client_id,
        )
    except Exception as error:
        raise GoogleLoginCredentialError(
            "유효하지 않은 Google 인증 정보입니다."
        ) from error

    issuer = str(payload.get("iss") or "").strip()
    audience = str(payload.get("aud") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    hosted_domain = str(payload.get("hd") or "").strip().lower()
    subject = str(payload.get("sub") or "").strip()
    email_domain = email.rsplit("@", maxsplit=1)[-1] if "@" in email else ""

    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise GoogleLoginCredentialError("유효하지 않은 Google 인증 정보입니다.")
    if audience != client_id or not subject:
        raise GoogleLoginCredentialError("유효하지 않은 Google 인증 정보입니다.")
    if payload.get("email_verified") is not True:
        raise GoogleLoginAccessError("검증된 Google Workspace 계정이 아닙니다.")
    if hosted_domain not in allowed_domains or email_domain not in allowed_domains:
        raise GoogleLoginAccessError("허용되지 않은 이메일 도메인입니다.")

    return {
        "sub": subject,
        "email": email,
        "hosted_domain": hosted_domain,
        "display_name": str(payload.get("name") or "").strip()[:200],
    }


def authenticate_google_user(db: Session, identity: dict[str, str]) -> UserModel:
    subject = identity["sub"]
    email = identity["email"]
    subject_user = db.scalar(select(UserModel).where(UserModel.google_sub == subject))
    email_user = db.scalar(
        select(UserModel).where(func.lower(UserModel.email) == email)
    )
    if subject_user is not None and email_user is not None and subject_user.id != email_user.id:
        raise GoogleLoginAccessError("Google 계정 연결 정보가 일치하지 않습니다.")

    user = subject_user or email_user
    if user is None:
        allowed_emails = {
            item.strip().lower()
            for item in settings.google_login_allowed_emails
            if item.strip()
        }
        if email not in allowed_emails:
            raise GoogleLoginAccessError("로그인이 허용된 사용자가 아닙니다.")
        salt, password_hash = hash_password(secrets.token_urlsafe(48))
        username = f"google_{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:32]}"
        user = UserModel(
            username=username,
            password_salt=salt,
            password_hash=password_hash,
            email=email,
            google_sub=subject,
            display_name=identity["display_name"] or None,
            role="viewer",
            is_active=True,
        )
        db.add(user)
        db.flush()
    if not user.is_active:
        raise GoogleLoginAccessError("로그인이 허용된 사용자가 아닙니다.")
    if user.google_sub is not None and user.google_sub != subject:
        raise GoogleLoginAccessError("Google 계정 연결 정보가 일치하지 않습니다.")

    user.google_sub = subject
    user.email = email
    user.display_name = identity["display_name"] or user.display_name
    user.last_login_at = datetime.now(timezone.utc)
    db.flush()
    return user


def initialize_google_user_workspace(
    db: Session,
    user: UserModel,
) -> tuple[OrganizationModel, OrganizationMemberModel]:
    existing_workspace = db.execute(
        select(OrganizationModel, OrganizationMemberModel)
        .join(
            OrganizationMemberModel,
            OrganizationMemberModel.organization_id == OrganizationModel.id,
        )
        .where(OrganizationMemberModel.user_id == user.id)
    ).one_or_none()
    if existing_workspace is not None:
        organization, membership = existing_workspace
        if not organization.is_active or not membership.is_active:
            raise GoogleLoginAccessError("활성 사용자 공간에 소속되지 않은 사용자입니다.")
    else:
        organization = db.scalar(
            select(OrganizationModel).where(
                OrganizationModel.slug == settings.default_organization_slug,
                OrganizationModel.is_active.is_(True),
            )
        )
        if organization is None:
            raise GoogleLoginConfigurationError("기본 사용자 공간이 설정되지 않았습니다.")
        membership = OrganizationMemberModel(
            organization_id=organization.id,
            user_id=user.id,
            role="admin" if user.role == "admin" else "member",
            is_active=True,
        )
        db.add(membership)
        db.flush()

    from app.g2b.opening_results.matching import get_user_result_profile

    get_user_result_profile(
        db,
        organization_id=organization.id,
        user_id=user.id,
    )
    db.flush()
    return organization, membership


def require_auth(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다.")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer 토큰 형식이 아닙니다.")

    token = authorization.split(" ", maxsplit=1)[1].strip()
    try:
        payload = parse_access_token(token)
        if not payload.get("username"):
            raise ValueError("invalid payload")
        user = db.get(UserModel, int(payload["sub"]))
        if user is None or not user.is_active or user.username != payload["username"]:
            raise ValueError("inactive user")
        return {
            "user_id": user.id,
            "username": user.username,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
            "iat": payload["iat"],
            "exp": payload["exp"],
        }
    except ValueError as error:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.") from error


def load_user_organization(db: Session, user_id: int) -> tuple[OrganizationModel, OrganizationMemberModel]:
    row = db.execute(
        select(OrganizationModel, OrganizationMemberModel)
        .join(
            OrganizationMemberModel,
            OrganizationMemberModel.organization_id == OrganizationModel.id,
        )
        .where(
            OrganizationMemberModel.user_id == user_id,
            OrganizationMemberModel.is_active.is_(True),
            OrganizationModel.is_active.is_(True),
        )
    ).one_or_none()
    if row is None:
        raise ValueError("활성 조직에 소속되지 않은 사용자입니다.")
    return row


def require_organization_auth(
    auth: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    try:
        organization, membership = load_user_organization(db, auth["user_id"])
    except ValueError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {
        **auth,
        "organization_id": organization.id,
        "organization_name": organization.name,
        "organization_role": membership.role,
    }


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


def verify_cloud_scheduler_oidc_token(
    authorization: str | None = Header(default=None),
) -> None:
    if settings.environment.strip().lower() in {"local", "test"}:
        return

    expected_audience = (
        settings.g2b_award_scheduler_oidc_audience.strip()
        or settings.g2b_award_scheduler_target_url.strip()
    )
    expected_email = settings.cloud_scheduler_invoker_service_account.strip().lower()
    if not expected_audience or not expected_email:
        raise HTTPException(
            status_code=503,
            detail="Cloud Scheduler OIDC 설정이 없습니다.",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="유효하지 않은 Scheduler 인증입니다.")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="유효하지 않은 Scheduler 인증입니다.")

    try:
        payload = google_id_token.verify_oauth2_token(
            token,
            GoogleRequest(),
            expected_audience,
        )
    except Exception as error:
        raise HTTPException(
            status_code=401,
            detail="유효하지 않은 Scheduler 인증입니다.",
        ) from error

    issuer = str(payload.get("iss") or "").strip()
    audience = str(payload.get("aud") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if (
        issuer not in {"accounts.google.com", "https://accounts.google.com"}
        or audience != expected_audience
        or email != expected_email
        or payload.get("email_verified") is not True
        or not str(payload.get("sub") or "").strip()
    ):
        raise HTTPException(status_code=401, detail="유효하지 않은 Scheduler 인증입니다.")
