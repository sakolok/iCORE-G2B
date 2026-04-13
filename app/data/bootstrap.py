from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models import LandingTemplateModel, ScraperConfigModel, UserModel
from app.services.auth_service import hash_password

DEFAULT_TEMPLATES = [
    LandingTemplateModel(
        id="clean-campaign",
        name="Clean Campaign",
        description="교육/설명형 랜딩에 맞는 심플한 구성",
        preview_style="left-copy-right-cta",
    ),
    LandingTemplateModel(
        id="dark-product",
        name="Dark Product",
        description="기술/솔루션 소개에 맞는 다크 톤 구성",
        preview_style="hero-centered-strong-cta",
    ),
    LandingTemplateModel(
        id="event-highlight",
        name="Event Highlight",
        description="모집/행사 공지에 맞는 카드형 구성",
        preview_style="headline-benefits-action",
    ),
]


def seed_defaults(db: Session) -> None:
    existing_template_ids = {
        row[0] for row in db.execute(select(LandingTemplateModel.id)).all()
    }
    for template in DEFAULT_TEMPLATES:
        if template.id not in existing_template_ids:
            db.add(template)

    config_exists = db.execute(select(ScraperConfigModel.id).limit(1)).scalar_one_or_none()
    if config_exists is None:
        db.add(
            ScraperConfigModel(
                enabled=True,
                notify_times="09:00:00",
                gsheet_id=None,
                receiver_emails=settings.default_receiver_email,
                keywords="클라우드,AI,교육",
            )
        )

    admin_exists = (
        db.execute(select(UserModel.id).where(UserModel.username == settings.default_admin_username))
        .scalar_one_or_none()
    )
    if admin_exists is None:
        salt, password_hash = hash_password(settings.default_admin_password)
        db.add(
            UserModel(
                username=settings.default_admin_username,
                password_salt=salt,
                password_hash=password_hash,
                role="admin",
                is_active=True,
            )
        )

    db.commit()
