"""Local, secret-free diagnostics for G2B preview queries.

This file stays inside the isolated G2B feature. It does not write to the
shared database model or create a migration.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
HISTORY_PATH = Path(__file__).resolve().parents[3] / ".local" / "g2b-query-history.jsonl"


def append_query_history(
    *,
    settings: Any,
    upstream_queries: list[dict[str, Any]],
    source_notice_ids: set[str],
) -> None:
    """Append one trace record without retaining an API key or request URL."""

    entry = {
        "query_id": uuid.uuid4().hex,
        "queried_at_kst": datetime.now(KST).isoformat(),
        "conditions": {
            "work_types": list(settings.work_types),
            "procurement_types": list(settings.procurement_types),
            "required_title_keywords": list(settings.required_title_keywords),
            "excluded_title_keywords": list(settings.excluded_title_keywords),
            "participation_regions": list(settings.participation_regions),
            "posted_date_start": (
                settings.posted_date_start.isoformat() if settings.posted_date_start else None
            ),
            "posted_date_end": settings.posted_date_end.isoformat() if settings.posted_date_end else None,
            "base_amount_min": settings.base_amount_min,
            "base_amount_max": settings.base_amount_max,
        },
        "upstream_queries": upstream_queries,
        "unique_source_notice_count": len(source_notice_ids),
        "source_notice_ids": sorted(source_notice_ids),
    }

    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a", encoding="utf-8") as history_file:
            history_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # Diagnostics must never prevent users from viewing a successful query.
        return
