from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import settings
from app.schemas import DeployRequest, DeployResponse


def create_static_deployment(request: DeployRequest) -> DeployResponse:
    deployment_id = str(uuid4())
    landing_page_id = str(uuid4())
    target_path = (
        f"gs://{settings.client_web_bucket}/landings/{request.business_topic}/{request.slug}/index.html"
    )

    if request.custom_domain:
        public_url = f"https://{request.custom_domain}"
    elif settings.landing_cdn_base_url:
        public_url = f"{settings.landing_cdn_base_url.rstrip('/')}/landings/{request.business_topic}/{request.slug}/"
    else:
        public_url = f"https://storage.googleapis.com/{settings.client_web_bucket}/landings/{request.business_topic}/{request.slug}/"

    timestamp = datetime.now(timezone.utc).isoformat()
    message = (
        "정적 랜딩 페이지 배포 요청이 생성되었습니다. "
        f"Cloud Build 파이프라인이 {timestamp} 에 대상 경로 업로드를 수행하도록 설계되었습니다."
    )

    return DeployResponse(
        deployment_id=deployment_id,
        landing_page_id=landing_page_id,
        target_path=target_path,
        public_url=public_url,
        cdn_enabled=True,
        message=message,
    )
