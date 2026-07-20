import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.g2b.pre_specifications.models import PreSpecificationModel
from app.g2b.pre_specifications.service import deadline_status


SHEET_HEADERS = ["사전규격 등록번호", "사업명", "수요기관", "공고기관", "사업구분", "배정예산", "등록일", "의견마감일", "의견마감 상태", "담당자", "연락처", "첨부문서 URL"]


class PreSpecificationSheetError(RuntimeError):
    pass


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    value = value.replace(tzinfo=ZoneInfo("Asia/Seoul")) if value.tzinfo is None else value
    return value.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


def _attachments(row: PreSpecificationModel) -> str:
    try:
        values = json.loads(row.attachments_json or "[]")
    except json.JSONDecodeError:
        return ""
    return "\n".join(str(item.get("url") or "").strip() for item in values if isinstance(item, dict) and str(item.get("url") or "").strip())


def build_rows(rows: list[PreSpecificationModel]) -> list[list[str | int | float]]:
    return [[row.bf_spec_rgst_no, row.business_name or "", row.demand_agency_name or "", row.ordering_agency_name or "", row.business_type or "", int(row.allocated_budget) if isinstance(row.allocated_budget, Decimal) and row.allocated_budget == row.allocated_budget.to_integral_value() else float(row.allocated_budget) if row.allocated_budget is not None else "", _format_datetime(row.registered_at), _format_datetime(row.opinion_deadline), deadline_status(row.opinion_deadline), row.contact_name or "", row.contact_phone or "", _attachments(row)] for row in rows]


class PreSpecificationSheetWriter:
    def __init__(self, spreadsheet_id: str, tab_name: str, service: Any | None = None):
        self.spreadsheet_id = spreadsheet_id.strip()
        self.tab_name = tab_name.strip() or "사전규격"
        if not self.spreadsheet_id:
            raise PreSpecificationSheetError("Google Spreadsheet ID가 필요합니다.")
        self.service = service or self._build_service()

    @staticmethod
    def _build_service():
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as error:
            raise PreSpecificationSheetError("Google Sheets 클라이언트 의존성이 설치되지 않았습니다.") from error
        inline_json = os.getenv("GSHEET_SERVICE_ACCOUNT_JSON", "").strip()
        if inline_json:
            try:
                credentials = service_account.Credentials.from_service_account_info(json.loads(inline_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
            except (ValueError, KeyError) as error:
                raise PreSpecificationSheetError("GSHEET_SERVICE_ACCOUNT_JSON 형식이 올바르지 않습니다.") from error
            return build("sheets", "v4", credentials=credentials, cache_discovery=False)
        try:
            import google.auth
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
            return build("sheets", "v4", credentials=credentials, cache_discovery=False)
        except Exception as error:
            raise PreSpecificationSheetError("Google 서비스 계정 또는 ADC 인증정보를 설정해 주세요.") from error

    def upsert(self, rows: list[list[str | int | float]]) -> int:
        tab = self.tab_name.replace("'", "''")
        values = self.service.spreadsheets().values()
        header = values.get(spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A1:L1").execute().get("values") or []
        if not header:
            values.update(spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A1", valueInputOption="RAW", body={"values": [SHEET_HEADERS]}).execute()
        elif header[0][:len(SHEET_HEADERS)] != SHEET_HEADERS:
            raise PreSpecificationSheetError("사전규격 Sheet의 A:L 헤더가 고정 12개 열과 일치하지 않습니다.")
        existing = values.get(spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A2:A").execute().get("values") or []
        known = {str(value[0]).strip() for value in existing if value and str(value[0]).strip()}
        append_rows = [row for row in rows if str(row[0]).strip() not in known]
        if append_rows:
            values.append(spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A:L", valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": append_rows}).execute()
        return len(append_rows)

    def existing_ids(self) -> set[str]:
        tab = self.tab_name.replace("'", "''")
        values = self.service.spreadsheets().values()
        rows = values.get(
            spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A2:A"
        ).execute().get("values") or []
        return {str(row[0]).strip() for row in rows if row and str(row[0]).strip()}
