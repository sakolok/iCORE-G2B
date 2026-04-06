import json
from datetime import time

from app.core.config import settings
from app.schemas import ScraperConfig, SchedulerStatus

try:
    from google.cloud import scheduler_v1
except Exception:  # pragma: no cover
    scheduler_v1 = None


def _configured() -> bool:
    required = [
        settings.cloud_scheduler_project_id,
        settings.cloud_scheduler_location,
        settings.cloud_scheduler_job_id,
        settings.cloud_scheduler_target_url,
    ]
    return settings.cloud_scheduler_enabled and all(item.strip() for item in required)


def _build_schedule(config: ScraperConfig) -> str:
    if config.schedule_mode == "interval":
        return f"every {config.interval_minutes} minutes"

    notify_time: time = config.notify_time
    return f"{notify_time.minute} {notify_time.hour} * * *"


def _job_name() -> str:
    return (
        f"projects/{settings.cloud_scheduler_project_id}"
        f"/locations/{settings.cloud_scheduler_location}"
        f"/jobs/{settings.cloud_scheduler_job_id}"
    )


def _build_body(config: ScraperConfig) -> bytes:
    payload = {
        "enabled": config.enabled,
        "schedule_mode": config.schedule_mode,
        "notify_time": config.notify_time.isoformat(),
        "interval_minutes": config.interval_minutes,
        "dedup_mode": config.dedup_mode,
        "dedup_retention_hours": config.dedup_retention_hours,
        "receiver_emails": config.receiver_emails,
        "keywords": config.keywords,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _build_http_target(config: ScraperConfig):
    headers = {"Content-Type": "application/json"}
    target = scheduler_v1.HttpTarget(
        uri=settings.cloud_scheduler_target_url,
        http_method=scheduler_v1.HttpMethod.POST,
        headers=headers,
        body=_build_body(config),
    )

    if settings.cloud_scheduler_invoker_service_account:
        target.oidc_token = scheduler_v1.OidcToken(
            service_account_email=settings.cloud_scheduler_invoker_service_account,
            audience=settings.cloud_scheduler_target_url,
        )

    return target


def get_scheduler_status(config: ScraperConfig) -> SchedulerStatus:
    schedule = _build_schedule(config)
    if not _configured():
        return SchedulerStatus(
            configured=False,
            connected=False,
            applied=False,
            paused=not config.enabled,
            schedule=schedule,
            job_name="",
            target_url=settings.cloud_scheduler_target_url,
            message="Cloud Scheduler 연동이 비활성화되어 있습니다.",
        )

    if scheduler_v1 is None:
        return SchedulerStatus(
            configured=True,
            connected=False,
            applied=False,
            paused=not config.enabled,
            schedule=schedule,
            job_name=_job_name(),
            target_url=settings.cloud_scheduler_target_url,
            message="google-cloud-scheduler 패키지가 없어 상태를 확인할 수 없습니다.",
        )

    client = scheduler_v1.CloudSchedulerClient()
    try:
        job = client.get_job(name=_job_name())
        paused = (
            job.state == scheduler_v1.Job.State.PAUSED
            if job.state is not None
            else not config.enabled
        )
        return SchedulerStatus(
            configured=True,
            connected=True,
            applied=True,
            paused=paused,
            schedule=job.schedule or schedule,
            job_name=job.name,
            target_url=job.http_target.uri if job.http_target else settings.cloud_scheduler_target_url,
            message="Cloud Scheduler 잡이 연결되어 있습니다.",
        )
    except Exception as exc:
        return SchedulerStatus(
            configured=True,
            connected=False,
            applied=False,
            paused=not config.enabled,
            schedule=schedule,
            job_name=_job_name(),
            target_url=settings.cloud_scheduler_target_url,
            message=f"Cloud Scheduler 상태 조회 실패: {exc}",
        )


def sync_scheduler_job(config: ScraperConfig) -> SchedulerStatus:
    schedule = _build_schedule(config)
    if not _configured():
        return get_scheduler_status(config)

    if scheduler_v1 is None:
        return get_scheduler_status(config)

    client = scheduler_v1.CloudSchedulerClient()
    parent = (
        f"projects/{settings.cloud_scheduler_project_id}"
        f"/locations/{settings.cloud_scheduler_location}"
    )
    name = _job_name()

    try:
        client.get_job(name=name)
        job = scheduler_v1.Job(
            name=name,
            schedule=schedule,
            time_zone=settings.cloud_scheduler_timezone,
            http_target=_build_http_target(config),
        )
        update_mask = {"paths": ["schedule", "time_zone", "http_target"]}
        client.update_job(job=job, update_mask=update_mask)
    except Exception:
        job = scheduler_v1.Job(
            name=name,
            schedule=schedule,
            time_zone=settings.cloud_scheduler_timezone,
            http_target=_build_http_target(config),
        )
        client.create_job(parent=parent, job=job)

    try:
        if config.enabled:
            client.resume_job(name=name)
        else:
            client.pause_job(name=name)
    except Exception:
        pass

    status = get_scheduler_status(config)
    status.applied = True
    status.message = "Cloud Scheduler 설정이 반영되었습니다."
    return status


def run_scheduler_job_now(config: ScraperConfig, reason: str | None):
    if not _configured() or scheduler_v1 is None:
        return None

    client = scheduler_v1.CloudSchedulerClient()
    name = _job_name()
    try:
        sync_scheduler_job(config)
        client.run_job(name=name)
        return {
            "ran": True,
            "job_name": name,
            "reason": reason or "manual",
        }
    except Exception:
        return None
