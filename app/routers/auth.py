import ipaddress

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.database import get_db
from app.data.models import UserModel
from app.schemas import GoogleLoginRequest, LoginRequest, LoginResponse, SessionResponse
from app.services.auth_service import (
    GoogleLoginAccessError,
    GoogleLoginConfigurationError,
    GoogleLoginCredentialError,
    authenticate_google_user,
    authenticate_user,
    create_access_token,
    initialize_google_user_workspace,
    load_user_organization,
    require_organization_auth,
    verify_google_identity,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login_response(user, organization, membership) -> LoginResponse:
    token = create_access_token(user_id=user.id, username=user.username, role=user.role)
    return LoginResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        organization_id=organization.id,
        organization_name=organization.name,
        organization_role=membership.role,
    )


@router.post("/single-user", response_model=LoginResponse)
def single_user_session(
    request: Request,
    db: Session = Depends(get_db),
) -> LoginResponse:
    if (
        not settings.single_user_mode_enabled
        or settings.environment.strip().lower() not in {"local", "test"}
    ):
        raise HTTPException(
            status_code=403,
            detail="단일 사용자 모드가 비활성화되어 있습니다.",
        )

    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(
            status_code=403,
            detail="단일 사용자 모드는 로컬 접속에서만 사용할 수 있습니다.",
        )

    username = settings.single_user_username.strip()
    if not username:
        raise HTTPException(status_code=503, detail="기본 사용자가 설정되지 않았습니다.")

    user = db.scalar(
        select(UserModel).where(
            UserModel.username == username,
            UserModel.is_active.is_(True),
        )
    )
    if user is None:
        raise HTTPException(
            status_code=503,
            detail="기본 사용자를 찾을 수 없습니다.",
        )

    try:
        organization, membership = load_user_organization(db, user.id)
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return _login_response(user, organization, membership)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    if not settings.legacy_password_login_enabled:
        raise HTTPException(
            status_code=403,
            detail="Google Workspace 계정으로 로그인해 주세요.",
        )
    try:
        user = authenticate_user(db, payload.username, payload.password)
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error

    try:
        organization, membership = load_user_organization(db, user.id)
    except ValueError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return _login_response(user, organization, membership)


@router.post("/google", response_model=LoginResponse)
def google_login(payload: GoogleLoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    try:
        identity = verify_google_identity(payload.credential)
        try:
            user = authenticate_google_user(db, identity)
            organization, membership = initialize_google_user_workspace(db, user)
            db.commit()
            db.refresh(user)
        except IntegrityError:
            db.rollback()
            user = authenticate_google_user(db, identity)
            organization, membership = initialize_google_user_workspace(db, user)
            db.commit()
            db.refresh(user)
    except GoogleLoginConfigurationError as error:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(error)) from error
    except GoogleLoginCredentialError as error:
        db.rollback()
        raise HTTPException(status_code=401, detail=str(error)) from error
    except (GoogleLoginAccessError, ValueError) as error:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="로그인 계정을 준비하는 중 충돌이 발생했습니다. 다시 시도해 주세요.",
        ) from error

    return _login_response(user, organization, membership)


@router.get("/me", response_model=SessionResponse)
def me(auth: dict = Depends(require_organization_auth)) -> SessionResponse:
    return SessionResponse(**auth)
