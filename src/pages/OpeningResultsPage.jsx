import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
  notification,
} from "antd";
import dayjs from "dayjs";
import { formatApiError, openingResultsApi } from "../api/client";
import "./OpeningResultsPage.css";

const { RangePicker } = DatePicker;

const STATUS_META = {
  OPENED: { color: "blue", label: "개찰완료" },
  AWARDED: { color: "green", label: "낙찰확정" },
  FAILED: { color: "red", label: "유찰" },
  REBID: { color: "gold", label: "재입찰" },
  CANCELLED: { color: "default", label: "취소" },
  UNKNOWN: { color: "default", label: "확인 필요" },
};

const EXPORT_STATUS_META = {
  READY: { color: "green", label: "반영 가능" },
  DETAIL_PENDING: { color: "gold", label: "상세 수집 대기" },
  NOTICE_CONTEXT_MISSING: { color: "red", label: "공고정보 누락" },
  NOTICE_CONTEXT_AMBIGUOUS: { color: "volcano", label: "공고정보 중복" },
};

const BLOCK_REASON_LABELS = {
  entries_collected_at: "업체별 순위·점수 상세가 아직 수집되지 않았습니다.",
  ambiguous_bid_notice_context: "같은 공고키의 공식 공고정보가 여러 건입니다.",
  bid_notice_context: "연결된 공식 입찰공고 정보가 없습니다.",
  business_name: "공식 사업명이 없습니다.",
  demand_agency_name: "공식 수요기관명이 없습니다.",
  base_amount: "사업금액 정보가 없습니다.",
  prearranged_price_decision_method: "예정가격 결정방법이 없습니다.",
  proposal_deadline: "제안마감 정보가 없습니다.",
  region_restriction: "지역제한 정보 확인이 필요합니다.",
  is_two_stage_bid: "2단계 입찰 여부가 없습니다.",
};

const HEADER_STATUS_META = {
  MATCH: { type: "success", text: "A:Q 헤더가 올바릅니다." },
  EMPTY: { type: "success", text: "빈 탭입니다. 첫 반영 시 고정 헤더를 만듭니다." },
  MISMATCH: {
    type: "error",
    text: "기존 헤더가 개찰결과 17개 열과 다릅니다. 빈 탭이나 올바른 헤더의 탭을 사용하세요.",
  },
  NOT_CHECKED: { type: "warning", text: "탭을 확인하지 못했습니다." },
};

const MAX_SELECTION_COUNT = 100;

function businessTitle(row) {
  return row?.business_name || row?.title || "사업명 미확인";
}

function sheetUrl(spreadsheetId) {
  return spreadsheetId
    ? `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit`
    : null;
}

function externalHttpUrl(value) {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    const hostname = parsed.hostname.toLowerCase();
    const isOfficialG2bHost = hostname === "g2b.go.kr" || hostname.endsWith(".g2b.go.kr");
    return ["http:", "https:"].includes(parsed.protocol) && isOfficialG2bHost
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}

function archiveDaysRemaining(expiresAt) {
  if (!expiresAt) return 0;
  return Math.max(0, Math.ceil(dayjs(expiresAt).diff(dayjs(), "hour", true) / 24));
}

function formatMoney(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString("ko-KR")}원` : String(value);
}

function regionRestrictionText(row) {
  if (row?.region_restriction_api_status === "API_EMPTY") {
    return "확인 필요 (API 응답 비어 있음)";
  }
  if (row?.region_restriction_api_status === "API_ERROR") {
    return "재확인 대기 (API 오류)";
  }
  if (row?.region_restriction_api_status === "ORDER_MISMATCH") {
    return "확인 필요 (공고차수 불일치)";
  }
  return row?.region_restriction || "확인 필요";
}

function exportStatusMeta(value, row) {
  if (value === "NOTICE_CONTEXT_MISSING") {
    if (row?.region_restriction_api_status === "API_ERROR") {
      return { color: "gold", label: "재확인 대기" };
    }
    if (["API_EMPTY", "ORDER_MISMATCH"].includes(row?.region_restriction_api_status)) {
      return { color: "orange", label: "확인 필요" };
    }
  }
  return EXPORT_STATUS_META[value] || EXPORT_STATUS_META.NOTICE_CONTEXT_MISSING;
}

function decimalParts(value) {
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(String(value).trim());
  if (!match) return null;
  const fraction = match[3] || "";
  const magnitude = BigInt(`${match[2]}${fraction}`);
  return {
    integer: match[1] === "-" ? -magnitude : magnitude,
    scale: fraction.length,
  };
}

function formatScoreComponent(value) {
  const text = String(value).trim();
  if (!text.includes(".")) return text;
  const normalized = text.replace(/0+$/, "").replace(/\.$/, "");
  return normalized === "-0" ? "0" : normalized;
}

function addAndRoundScore(priceValue, technicalValue) {
  const price = decimalParts(priceValue);
  const technical = decimalParts(technicalValue);
  if (!price || !technical) return null;
  const scale = Math.max(price.scale, technical.scale);
  const scaledPrice = price.integer * 10n ** BigInt(scale - price.scale);
  const scaledTechnical = technical.integer * 10n ** BigInt(scale - technical.scale);
  const sum = scaledPrice + scaledTechnical;
  const negative = sum < 0n;
  const absolute = negative ? -sum : sum;
  let cents;
  if (scale <= 2) {
    cents = absolute * 10n ** BigInt(2 - scale);
  } else {
    const divisor = 10n ** BigInt(scale - 2);
    cents = absolute / divisor;
    if ((absolute % divisor) * 2n >= divisor) cents += 1n;
  }
  const whole = cents / 100n;
  const fraction = String(cents % 100n).padStart(2, "0");
  return `${negative ? "-" : ""}${whole}.${fraction}`;
}

function formatScoreExpression(entry) {
  if (entry?.bid_price_score == null || entry?.technical_score == null) return "";
  const roundedTotal = addAndRoundScore(entry.bid_price_score, entry.technical_score);
  if (roundedTotal == null) return "";
  return `${formatScoreComponent(entry.bid_price_score)}+${formatScoreComponent(entry.technical_score)}=${roundedTotal}`;
}

function exportBlockText(row) {
  const reasons = row?.sheet_block_reasons || [];
  if (!reasons.length) return "";
  return reasons.map((reason) => BLOCK_REASON_LABELS[reason] || reason).join(" ");
}

function OpeningResultsPage() {
  const resultsRequestId = useRef(0);
  const detailRequestId = useRef(0);
  const archiveRequestId = useRef(0);
  const archivePageRef = useRef(1);
  const archivePageSizeRef = useRef(30);
  const destinationVerifyRequestId = useRef(0);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(30);
  const [loading, setLoading] = useState(false);
  const [resultsError, setResultsError] = useState("");
  const [lastLoadedAt, setLastLoadedAt] = useState(null);
  const [queryDraft, setQueryDraft] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState(null);
  const [readinessFilter, setReadinessFilter] = useState("ALL");
  const [dateRange, setDateRange] = useState(null);
  const [selectedById, setSelectedById] = useState(() => new Map());
  const [selectionOpen, setSelectionOpen] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [writing, setWriting] = useState(false);
  const [previewData, setPreviewData] = useState(null);
  const [previewTarget, setPreviewTarget] = useState(null);
  const [previewError, setPreviewError] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveRows, setArchiveRows] = useState([]);
  const [archiveTotal, setArchiveTotal] = useState(0);
  const [archivePage, setArchivePage] = useState(1);
  const [archivePageSize, setArchivePageSize] = useState(30);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [archiveError, setArchiveError] = useState("");
  const [restoringId, setRestoringId] = useState(null);
  const [settings, setSettings] = useState(null);
  const [settingsError, setSettingsError] = useState("");
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileEnabled, setProfileEnabled] = useState(false);
  const [keywords, setKeywords] = useState([]);
  const [excludedKeywords, setExcludedKeywords] = useState([]);
  const [savingProfile, setSavingProfile] = useState(false);
  const [destinationId, setDestinationId] = useState(null);
  const [destinationOpen, setDestinationOpen] = useState(false);
  const [savingDestination, setSavingDestination] = useState(false);
  const [verifyingDestination, setVerifyingDestination] = useState(false);
  const [connectionResult, setConnectionResult] = useState(null);
  const [connectionError, setConnectionError] = useState("");
  const [destinationDraft, setDestinationDraft] = useState({
    label: "내 개찰결과 Sheet",
    spreadsheet_id: "",
    tab_name: "개찰결과",
    scope: "PERSONAL",
  });
  archivePageRef.current = archivePage;
  archivePageSizeRef.current = archivePageSize;

  const selectedRows = useMemo(() => [...selectedById.values()], [selectedById]);
  const selectedRowKeys = useMemo(() => [...selectedById.keys()], [selectedById]);
  const usableDestinations = useMemo(
    () => settings?.sheet_destinations || [],
    [settings]
  );
  const selectedDestination = useMemo(
    () => usableDestinations.find((item) => item.id === destinationId),
    [destinationId, usableDestinations]
  );
  const filtersApplied = Boolean(appliedQuery || statusFilter || dateRange || readinessFilter !== "ALL");

  const loadResults = async (nextPage = page, nextPageSize = pageSize) => {
    const requestId = ++resultsRequestId.current;
    setLoading(true);
    setResultsError("");
    try {
      const response = await openingResultsApi.list({
        page: nextPage,
        page_size: nextPageSize,
        q: appliedQuery || undefined,
        status: statusFilter || undefined,
        sheet_export_status: readinessFilter === "ALL" ? undefined : readinessFilter,
        opened_from: dateRange?.[0]?.startOf("day").toISOString(),
        opened_to: dateRange?.[1]?.endOf("day").toISOString(),
      });
      if (requestId !== resultsRequestId.current) return;
      const nextRows = response.data.items || [];
      setRows(nextRows);
      setTotal(response.data.total || 0);
      setLastLoadedAt(dayjs());
      setSelectedById((current) => {
        let changed = false;
        const next = new Map(current);
        nextRows.forEach((row) => {
          if (next.has(row.id)) {
            next.set(row.id, row);
            changed = true;
          }
        });
        return changed ? next : current;
      });
    } catch (error) {
      if (requestId !== resultsRequestId.current) return;
      setResultsError(formatApiError(error, "개찰결과 조회에 실패했습니다."));
      setRows([]);
      setTotal(0);
    } finally {
      if (requestId === resultsRequestId.current) setLoading(false);
    }
  };

  const loadArchive = async (
    nextPage = archivePage,
    nextPageSize = archivePageSize
  ) => {
    const requestId = ++archiveRequestId.current;
    setArchiveLoading(true);
    setArchiveError("");
    try {
      const response = await openingResultsApi.listArchive({
        page: nextPage,
        page_size: nextPageSize,
      });
      if (requestId !== archiveRequestId.current) return;
      const nextTotal = response.data.total || 0;
      const maxPage = Math.max(1, Math.ceil(nextTotal / nextPageSize));
      setArchiveTotal(nextTotal);
      if (nextPage > maxPage) {
        archivePageRef.current = maxPage;
        setArchivePage(maxPage);
        return;
      }
      setArchiveRows(response.data.items || []);
    } catch (error) {
      if (requestId !== archiveRequestId.current) return;
      setArchiveRows([]);
      setArchiveTotal(0);
      setArchiveError(formatApiError(error, "14일 보관함 조회에 실패했습니다."));
    } finally {
      if (requestId === archiveRequestId.current) setArchiveLoading(false);
    }
  };

  const loadSettings = async () => {
    setSettingsError("");
    try {
      const response = await openingResultsApi.settings();
      const nextSettings = response.data;
      setSettings(nextSettings);
      setProfileEnabled(nextSettings.profile?.enabled ?? false);
      setKeywords(nextSettings.profile?.keywords || []);
      setExcludedKeywords(nextSettings.profile?.excluded_keywords || []);
      const destinations = nextSettings.sheet_destinations || [];
      setDestinationId((current) => {
        if (destinations.some((item) => item.id === current)) return current;
        return destinations.find((item) => item.is_default)?.id ?? destinations[0]?.id ?? null;
      });
      return nextSettings;
    } catch (error) {
      setSettingsError(formatApiError(error, "개찰결과 설정 조회에 실패했습니다."));
      return null;
    }
  };

  useEffect(() => {
    loadResults(page, pageSize);
  }, [page, pageSize, appliedQuery, statusFilter, dateRange, readinessFilter]);

  useEffect(() => {
    loadSettings();
  }, []);

  useEffect(() => {
    if (archiveOpen) loadArchive(archivePage, archivePageSize);
  }, [archiveOpen, archivePage, archivePageSize]);

  const clearSelection = () => setSelectedById(new Map());

  const manualRefresh = async () => {
    if (selectedById.size) {
      clearSelection();
      message.info("DB 목록을 새로 읽어 선택 바구니를 비웠습니다.");
    }
    await Promise.all([loadResults(page, pageSize), loadSettings()]);
  };

  const resetFilters = () => {
    setQueryDraft("");
    setAppliedQuery("");
    setStatusFilter(null);
    setReadinessFilter("ALL");
    setDateRange(null);
    setPage(1);
  };

  const applySearch = (value) => {
    setQueryDraft(value);
    setAppliedQuery(value.trim());
    setPage(1);
  };

  const toggleSelected = (row, checked) => {
    if (checked && !selectedById.has(row.id) && selectedById.size >= MAX_SELECTION_COUNT) {
      message.warning(`한 번에 최대 ${MAX_SELECTION_COUNT}건까지 선택할 수 있습니다.`);
      return;
    }
    setSelectedById((current) => {
      const next = new Map(current);
      if (checked) next.set(row.id, row);
      else next.delete(row.id);
      return next;
    });
  };

  const togglePageSelection = (checked, _selectedRows, changeRows) => {
    const next = new Map(selectedById);
    let skipped = 0;
    changeRows.forEach((row) => {
      if (!checked) {
        next.delete(row.id);
      } else if (row.sheet_exportable && next.size < MAX_SELECTION_COUNT) {
        next.set(row.id, row);
      } else if (row.sheet_exportable) {
        skipped += 1;
      }
    });
    setSelectedById(next);
    if (skipped) {
      message.warning(`${MAX_SELECTION_COUNT}건을 초과한 ${skipped}건은 선택하지 않았습니다.`);
    }
  };

  const saveProfile = async () => {
    if (profileEnabled && keywords.length === 0) {
      message.warning("활성화할 때는 포함 키워드를 한 개 이상 입력하세요.");
      return;
    }
    setSavingProfile(true);
    try {
      await openingResultsApi.updateProfile({
        enabled: profileEnabled,
        keywords,
        excluded_keywords: excludedKeywords,
      });
      detailRequestId.current += 1;
      setDetailLoading(false);
      setDetail(null);
      clearSelection();
      setProfileOpen(false);
      setPage(1);
      await Promise.all([loadSettings(), loadResults(1, pageSize)]);
      message.success("내 키워드 조건을 저장하고 선택 바구니를 비웠습니다.");
    } catch (error) {
      message.error(formatApiError(error, "개찰결과 조건 저장에 실패했습니다."));
    } finally {
      setSavingProfile(false);
    }
  };

  const changeDestinationDraft = (field, value) => {
    setDestinationDraft((current) => ({ ...current, [field]: value }));
    if (field === "spreadsheet_id" || field === "tab_name") {
      destinationVerifyRequestId.current += 1;
      setVerifyingDestination(false);
      setConnectionResult(null);
      setConnectionError("");
    }
  };

  const openDestinationModal = () => {
    destinationVerifyRequestId.current += 1;
    setDestinationDraft({
      label: "내 개찰결과 Sheet",
      spreadsheet_id: "",
      tab_name: "개찰결과",
      scope: "PERSONAL",
    });
    setConnectionResult(null);
    setConnectionError("");
    setVerifyingDestination(false);
    setDestinationOpen(true);
  };

  const closeDestinationModal = () => {
    destinationVerifyRequestId.current += 1;
    setVerifyingDestination(false);
    setDestinationOpen(false);
  };

  const verifyDestination = async () => {
    if (!destinationDraft.spreadsheet_id.trim() || !destinationDraft.tab_name.trim()) {
      message.warning("Google Sheet URL 또는 ID와 탭 이름을 입력하세요.");
      return;
    }
    const requestId = ++destinationVerifyRequestId.current;
    const requestedSpreadsheetId = destinationDraft.spreadsheet_id;
    const requestedTabName = destinationDraft.tab_name;
    setVerifyingDestination(true);
    setConnectionError("");
    try {
      const response = await openingResultsApi.verifySheetDestination({
        spreadsheet_id: requestedSpreadsheetId,
        tab_name: requestedTabName,
      });
      if (requestId !== destinationVerifyRequestId.current) return;
      setDestinationDraft((current) => ({
        ...current,
        spreadsheet_id: response.data.spreadsheet_id,
      }));
      setConnectionResult(response.data);
      if (response.data.connection_ready) {
        message.success("읽기 전용 연결 테스트를 통과했습니다.");
      }
    } catch (error) {
      if (requestId !== destinationVerifyRequestId.current) return;
      setConnectionResult(null);
      setConnectionError(
        formatApiError(error, "Google Sheet 연결 확인에 실패했습니다. 공유 권한을 확인하세요.")
      );
    } finally {
      if (requestId === destinationVerifyRequestId.current) {
        setVerifyingDestination(false);
      }
    }
  };

  const destinationVerified = Boolean(
    connectionResult?.connection_ready &&
      connectionResult.spreadsheet_id === destinationDraft.spreadsheet_id.trim() &&
      connectionResult.tab_name === destinationDraft.tab_name.trim()
  );

  const saveDestination = async () => {
    if (!destinationDraft.label.trim()) {
      message.warning("Sheet 표시 이름을 입력하세요.");
      return;
    }
    if (!destinationVerified) {
      message.warning("현재 URL과 탭으로 연결 테스트를 먼저 통과하세요.");
      return;
    }
    const existingDestination = usableDestinations.find(
      (item) =>
        item.spreadsheet_id === connectionResult.spreadsheet_id &&
        item.tab_name === connectionResult.tab_name
    );
    if (existingDestination) {
      setDestinationId(existingDestination.id);
      closeDestinationModal();
      message.info("이미 등록된 Google Sheet 목적지를 선택했습니다.");
      return;
    }
    setSavingDestination(true);
    try {
      const response = await openingResultsApi.saveSheetDestination({
        ...destinationDraft,
        is_default: true,
      });
      await loadSettings();
      setDestinationId(response.data.id);
      closeDestinationModal();
      message.success("검증된 Google Sheet 목적지를 저장했습니다.");
    } catch (error) {
      if (error?.response?.status === 409) {
        const nextSettings = await loadSettings();
        const existingDestination = (nextSettings?.sheet_destinations || []).find(
          (item) =>
            item.spreadsheet_id === connectionResult?.spreadsheet_id &&
            item.tab_name === connectionResult?.tab_name
        );
        if (existingDestination) {
          setDestinationId(existingDestination.id);
          closeDestinationModal();
          message.info("이미 등록된 Google Sheet 목적지를 선택했습니다.");
          return;
        }
      }
      message.error(formatApiError(error, "Google Sheet 목적지 저장에 실패했습니다."));
    } finally {
      setSavingDestination(false);
    }
  };

  const deleteDestination = async (target) => {
    try {
      await openingResultsApi.deleteSheetDestination(target.id);
      await loadSettings();
      message.success("Google Sheet 연결을 목록에서 제거했습니다.");
    } catch (error) {
      message.error(formatApiError(error, "Google Sheet 연결 제거에 실패했습니다."));
    }
  };

  const copyServiceAccountEmail = async () => {
    const email = settings?.sheet_service_account_email;
    if (!email) return;
    try {
      await navigator.clipboard.writeText(email);
      message.success("서비스계정 이메일을 복사했습니다.");
    } catch {
      message.error("자동 복사에 실패했습니다. 이메일을 직접 선택해 복사하세요.");
    }
  };

  const openDetail = async (resultId, fromArchive = false) => {
    const requestId = ++detailRequestId.current;
    setDetailLoading(true);
    setDetail({ id: resultId, entries: [], from_archive: fromArchive });
    try {
      const response = fromArchive
        ? await openingResultsApi.archiveDetail(resultId)
        : await openingResultsApi.detail(resultId);
      if (requestId === detailRequestId.current) {
        setDetail({ ...response.data, from_archive: fromArchive });
      }
    } catch (error) {
      if (requestId === detailRequestId.current) {
        message.error(formatApiError(error, "업체별 순위 조회에 실패했습니다."));
        setDetail(null);
      }
    } finally {
      if (requestId === detailRequestId.current) setDetailLoading(false);
    }
  };

  const openPreview = async () => {
    if (!selectedById.size) {
      message.warning("Google Sheets에 반영할 결과를 선택하세요.");
      return;
    }
    if (!destinationId || !selectedDestination) {
      openDestinationModal();
      return;
    }
    const resultIds = [...selectedById.keys()];
    const target = {
      resultIds,
      destinationId,
      url: sheetUrl(selectedDestination.spreadsheet_id),
    };
    setPreviewing(true);
    setPreviewError("");
    try {
      const response = await openingResultsApi.exportSheet({
        result_ids: resultIds,
        destination_id: destinationId,
        dry_run: true,
      });
      if (response.data.missing_result_ids?.length) {
        throw new Error("선택한 결과 중 더 이상 조회할 수 없는 항목이 있습니다.");
      }
      if (response.data.missing_notice_context_count > 0) {
        throw new Error("공식 공고정보가 누락된 항목은 Sheet에 반영할 수 없습니다.");
      }
      setPreviewData(response.data);
      setPreviewTarget(target);
    } catch (error) {
      const text = error?.response
        ? formatApiError(error, "선택 결과 미리보기에 실패했습니다.")
        : error.message;
      message.error(text);
    } finally {
      setPreviewing(false);
    }
  };

  const confirmExport = async () => {
    if (!previewData || !previewTarget) return;
    setWriting(true);
    setPreviewError("");
    try {
      const response = await openingResultsApi.exportSheet({
        result_ids: previewTarget.resultIds,
        destination_id: previewTarget.destinationId,
        dry_run: false,
        expected_preview_token: previewData.preview_token,
      });
      setPreviewData(null);
      setPreviewTarget(null);
      clearSelection();
      await loadResults(page, pageSize);
      if (archiveOpen) await loadArchive(archivePage, archivePageSize);
      notification.success({
        message: "Google Sheets 반영 완료",
        description: `${response.data.inserted_count}건 추가, ${response.data.updated_count}건 갱신했습니다. 성공한 결과는 검토함에서 숨겨지고 14일 보관함에 남습니다.`,
        duration: 8,
        actions: previewTarget.url ? (
          <Button type="primary" href={previewTarget.url} target="_blank" rel="noreferrer">
            Sheet 열기
          </Button>
        ) : null,
      });
    } catch (error) {
      setPreviewError(
        formatApiError(
          error,
          "Google Sheet 반영에 실패했습니다. 선택은 유지되므로 원인을 확인한 뒤 다시 시도하세요."
        )
      );
    } finally {
      setWriting(false);
    }
  };

  const undoDismiss = async (resultId, notificationKey) => {
    try {
      const response = await openingResultsApi.restore(resultId);
      notification.destroy(notificationKey);
      await Promise.all([
        response.data.visible ? loadResults(page, pageSize) : Promise.resolve(),
        loadArchive(archivePageRef.current, archivePageSizeRef.current),
      ]);
      if (response.data.visible) {
        message.success("제외를 취소해 검토함에 다시 표시했습니다.");
      } else {
        message.warning("제외는 취소했지만 조직 공용 Sheet에서 이미 처리되어 다시 표시되지 않습니다.");
      }
    } catch (error) {
      message.error(formatApiError(error, "제외 실행취소에 실패했습니다."));
    }
  };

  const restoreArchiveRow = async (resultId) => {
    setRestoringId(resultId);
    try {
      const response = await openingResultsApi.restore(resultId);
      if (detail?.id === resultId) {
        detailRequestId.current += 1;
        setDetail(null);
        setDetailLoading(false);
      }
      await Promise.all([
        loadArchive(archivePage, archivePageSize),
        response.data.visible ? loadResults(page, pageSize) : Promise.resolve(),
      ]);
      if (response.data.visible) {
        message.success("검토할 결과로 복구했습니다.");
      } else {
        message.warning("제외 상태는 복구했지만 조직 공용 Sheet에서 이미 처리되어 목록에는 표시되지 않습니다.");
      }
    } catch (error) {
      message.error(formatApiError(error, "보관 항목 복구에 실패했습니다."));
    } finally {
      setRestoringId(null);
    }
  };

  const dismissRow = async (resultId) => {
    try {
      await openingResultsApi.dismiss(resultId);
      setRows((current) => current.filter((row) => row.id !== resultId));
      setTotal((current) => Math.max(0, current - 1));
      setSelectedById((current) => {
        const next = new Map(current);
        next.delete(resultId);
        return next;
      });
      if (detail?.id === resultId) {
        detailRequestId.current += 1;
        setDetailLoading(false);
        setDetail(null);
      }
      const notificationKey = `dismiss-${resultId}`;
      notification.open({
        key: notificationKey,
        message: "내 검토함에서 제외했습니다.",
        description: "14일 보관함에서 다시 확인하거나 검토할 결과로 복구할 수 있습니다.",
        duration: 10,
        actions: (
          <Button type="link" onClick={() => undoDismiss(resultId, notificationKey)}>
            실행취소
          </Button>
        ),
      });
    } catch (error) {
      message.error(formatApiError(error, "목록 제외에 실패했습니다."));
    }
  };

  const columns = [
    {
      title: "개찰일",
      dataIndex: "opened_at",
      width: 120,
      render: (value) => (value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "-"),
    },
    { title: "공고번호", dataIndex: "bid_notice_no", width: 132 },
    {
      title: "사업명 / 수요기관",
      key: "business",
      render: (_, row) => (
        <Space direction="vertical" size={2} className="opening-result-business">
          <Button type="link" onClick={() => openDetail(row.id)}>
            {businessTitle(row)}
          </Button>
          <Typography.Text type="secondary" ellipsis={{ tooltip: row.demand_agency_name }}>
            {row.demand_agency_name || "수요기관 미확인"}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "결과",
      dataIndex: "status",
      width: 72,
      render: (value) => {
        const meta = STATUS_META[value] || STATUS_META.UNKNOWN;
        return (
          <Tag className={`opening-status-tag is-${String(value || "unknown").toLowerCase()}`}>
            {meta.label}
          </Tag>
        );
      },
    },
    {
      title: "매칭",
      dataIndex: "matched_keywords",
      width: 88,
      render: (values = []) =>
        values.length ? values.map((value) => <Tag key={value}>{value}</Tag>) : "-",
    },
    {
      title: "개찰순위 1위",
      key: "first_rank",
      width: 130,
      render: (_, row) => (
        <Typography.Text ellipsis={{ tooltip: row.first_rank_company_name }}>
          {row.first_rank_company_name || "순위 미확인"}
        </Typography.Text>
      ),
    },
    {
      title: "최종 낙찰자",
      dataIndex: "winner_company_name",
      width: 126,
      ellipsis: true,
      render: (value) => value || "공식 확인 전",
    },
    {
      title: "Sheet 반영 상태",
      dataIndex: "sheet_export_status",
      width: 140,
      render: (value, row) => {
        const meta = exportStatusMeta(value, row);
        const reason = exportBlockText(row);
        return (
          <Space direction="vertical" size={2}>
            <Tag className={`opening-export-tag is-${String(value || "unknown").toLowerCase()}`}>
              {meta.label}
            </Tag>
            {reason ? (
              <Typography.Text type="secondary" className="opening-result-block-reason">
                {reason}
              </Typography.Text>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "작업",
      key: "actions",
      width: 82,
      render: (_, row) => (
        <Space size={2} className="opening-result-actions">
          <Button type="link" onClick={() => openDetail(row.id)}>
            상세
          </Button>
          <Popconfirm
            title="내 검토함에서 제외할까요?"
            description="14일 보관함으로 이동하며, 기간 안에는 다시 복구할 수 있습니다."
            okText="제외"
            cancelText="닫기"
            onConfirm={() => dismissRow(row.id)}
          >
            <Button type="link" danger>
              제외
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const archiveColumns = [
    {
      title: "처리일",
      dataIndex: "handled_at",
      width: 135,
      render: (value) => (value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "-"),
    },
    {
      title: "보관 유형",
      dataIndex: "handled_state",
      width: 120,
      render: (value) => (
        <Tag className={`opening-archive-tag is-${String(value || "unknown").toLowerCase()}`}>
          {value === "EXPORTED" ? "Sheet 반영" : "목록 제외"}
        </Tag>
      ),
    },
    {
      title: "사업명 / 공고번호",
      key: "business",
      render: (_, row) => (
        <Space direction="vertical" size={2} className="opening-result-business">
          <Button type="link" onClick={() => openDetail(row.id, true)}>
            {businessTitle(row)}
          </Button>
          <Typography.Text type="secondary">{row.bid_notice_no || "공고번호 미확인"}</Typography.Text>
        </Space>
      ),
    },
    {
      title: "자동 삭제",
      dataIndex: "expires_at",
      width: 135,
      render: (value) => {
        const days = archiveDaysRemaining(value);
        return (
          <Space direction="vertical" size={0}>
            <Typography.Text>{days > 0 ? `${days}일 남음` : "오늘 삭제"}</Typography.Text>
            <Typography.Text type="secondary">
              {value ? dayjs(value).format("MM-DD HH:mm") : "-"}
            </Typography.Text>
          </Space>
        );
      },
    },
    {
      title: "작업",
      key: "actions",
      width: 150,
      render: (_, row) => (
        <Space size={2}>
          <Button type="link" onClick={() => openDetail(row.id, true)}>
            상세
          </Button>
          {row.can_restore ? (
            <Popconfirm
              title="검토할 결과로 복구할까요?"
              description="복구하면 보관함에서 빠지고 원래 목록에 다시 표시됩니다."
              okText="복구"
              cancelText="취소"
              onConfirm={() => restoreArchiveRow(row.id)}
            >
              <Button type="link" loading={restoringId === row.id}>복구</Button>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ];

  const rankingColumns = [
    { title: "순위", dataIndex: "rank", width: 65 },
    { title: "업체명", dataIndex: "company_name", ellipsis: true },
    {
      title: "가격점수",
      dataIndex: "bid_price_score",
      width: 95,
      render: (value) => (value == null ? "" : formatScoreComponent(value)),
    },
    {
      title: "기술점수",
      dataIndex: "technical_score",
      width: 95,
      render: (value) => (value == null ? "" : formatScoreComponent(value)),
    },
    {
      title: "점수 계산식",
      key: "score_expression",
      width: 180,
      render: (_, entry) => formatScoreExpression(entry),
    },
  ];

  const selectionColumns = [
    { title: "공고번호", dataIndex: "bid_notice_no", width: 150 },
    { title: "사업명", key: "business_name", render: (_, row) => businessTitle(row) },
    {
      title: "선택 해제",
      key: "remove",
      width: 90,
      render: (_, row) => (
        <Button type="link" danger onClick={() => toggleSelected(row, false)}>
          해제
        </Button>
      ),
    },
  ];

  const canDeleteDestination = (destination) =>
    destination.scope === "PERSONAL" || settings?.organization_role === "admin";
  const connectionMeta = connectionResult
    ? HEADER_STATUS_META[connectionResult.header_status] || HEADER_STATUS_META.NOT_CHECKED
    : null;
  const detailStatus = STATUS_META[detail?.status] || STATUS_META.UNKNOWN;
  const detailFirstRank = (detail?.entries || []).find((entry) => entry.rank === 1);
  const detailEntriesPending = detail?.sheet_export_status === "DETAIL_PENDING";
  const detailNoticeUrl = externalHttpUrl(detail?.notice_url);

  return (
    <div className="opening-results-page">
      <section className="opening-results-hero">
        <div>
          <Typography.Text className="opening-results-eyebrow">
            최근 14일
          </Typography.Text>
          <Typography.Title level={2}>키워드에 맞는 개찰결과를 모았어요</Typography.Title>
          <Typography.Paragraph>
            내용을 확인하고 필요한 항목만 Google Sheets에 반영해요.
          </Typography.Paragraph>
        </div>
        <div className="opening-results-hero-actions">
          <Typography.Text type="secondary" className="opening-results-loaded-at">
            {lastLoadedAt ? `DB 조회 ${lastLoadedAt.format("MM-DD HH:mm:ss")}` : "DB 조회 전"}
          </Typography.Text>
          <Button onClick={() => setArchiveOpen(true)}>14일 보관함</Button>
          <Button onClick={manualRefresh} loading={loading}>
            목록 새로고침
          </Button>
          <Button type="primary" ghost onClick={() => setProfileOpen(true)}>조건 설정</Button>
        </div>
      </section>

      {settingsError ? (
        <Alert
          type="error"
          showIcon
          message="키워드·Sheet 설정을 불러오지 못했습니다."
          description={settingsError}
          action={<Button onClick={loadSettings}>다시 시도</Button>}
        />
      ) : null}

      {settings && !usableDestinations.length ? (
        <Alert
          type="warning"
          showIcon
          message="연결된 Google Sheet가 없습니다."
          description="서비스계정을 내 Sheet의 편집자로 공유하고 읽기 전용 연결 테스트를 통과하면 저장할 수 있습니다."
          action={
            <Button type="primary" onClick={openDestinationModal}>
              내 Sheet 연결
            </Button>
          }
        />
      ) : null}

      <Card className="opening-results-filter-card">
        <div className="opening-results-filter-heading">
          <div>
            <strong>결과 찾기</strong>
            <span>사업명, 공고번호, 기관, 업체를 검색할 수 있어요.</span>
          </div>
          <span>{filtersApplied ? "필터 적용 중" : "전체 결과"}</span>
        </div>
        <div className="opening-results-filter-row">
          <Input.Search
            allowClear
            value={queryDraft}
            onChange={(event) => setQueryDraft(event.target.value)}
            onSearch={applySearch}
            placeholder="사업명, 공고번호, 기관, 업체로 검색"
            enterButton="검색"
            className="opening-results-search"
          />
          <Select
            allowClear
            value={statusFilter}
            onChange={(value) => {
              setStatusFilter(value || null);
              setPage(1);
            }}
            placeholder="개찰 상태 전체"
            className="opening-results-filter-select"
            options={Object.entries(STATUS_META).map(([value, meta]) => ({
              value,
              label: meta.label,
            }))}
          />
          <Select
            value={readinessFilter}
            onChange={setReadinessFilter}
            className="opening-results-filter-select opening-results-readiness-filter"
            options={[
              { value: "ALL", label: "반영 상태 전체" },
              { value: "READY", label: "반영 가능" },
              { value: "DETAIL_PENDING", label: "상세 수집 대기" },
              { value: "NOTICE_CONTEXT_MISSING", label: "공고정보 누락" },
              { value: "NOTICE_CONTEXT_AMBIGUOUS", label: "공고정보 중복" },
              { value: "BLOCKED", label: "처리 대기 전체" },
            ]}
          />
          <RangePicker
            value={dateRange}
            onChange={(value) => {
              setDateRange(value);
              setPage(1);
            }}
            className="opening-results-date-filter"
          />
          <Button onClick={resetFilters} disabled={!filtersApplied}>
            필터 초기화
          </Button>
        </div>
        <div className="opening-results-condition-row">
          <Space wrap>
            <Typography.Text strong>내 포함 키워드</Typography.Text>
            {settings?.profile?.enabled ? (
              settings.profile.keywords?.map((keyword) => (
                <Tag className="opening-keyword-tag" key={keyword}>{keyword}</Tag>
              ))
            ) : (
              <Tag>사용 안 함</Tag>
            )}
            {settings?.profile?.excluded_keywords?.length ? (
              <>
                <Typography.Text strong>제외</Typography.Text>
                {settings.profile.excluded_keywords.map((keyword) => (
                  <Tag className="opening-keyword-tag is-excluded" key={keyword}>{keyword}</Tag>
                ))}
              </>
            ) : null}
          </Space>
          <Button type="link" onClick={() => setProfileOpen(true)}>
            조건 설정
          </Button>
        </div>
      </Card>

      <Card
        title="검토할 결과"
        extra={<Typography.Text type="secondary">총 {total.toLocaleString("ko-KR")}건</Typography.Text>}
        className="opening-results-table-card"
      >
        {resultsError ? (
          <Alert
            type="error"
            showIcon
            message="개찰결과를 불러오지 못했습니다."
            description={resultsError}
            action={<Button onClick={() => loadResults(page, pageSize)}>다시 시도</Button>}
          />
        ) : (
          <Table
            rowKey="id"
            loading={loading}
            dataSource={rows}
            columns={columns}
            tableLayout="fixed"
            rowClassName={(row) => [
              row.sheet_exportable ? "" : "opening-result-row-blocked",
              selectedById.has(row.id) ? "opening-result-row-selected" : "",
            ].filter(Boolean).join(" ")}
            rowSelection={{
              columnWidth: 44,
              preserveSelectedRowKeys: true,
              selectedRowKeys,
              onSelect: toggleSelected,
              onSelectAll: togglePageSelection,
              getCheckboxProps: (row) => ({
                disabled:
                  !row.sheet_exportable ||
                  (selectedById.size >= MAX_SELECTION_COUNT && !selectedById.has(row.id)),
                title: !row.sheet_exportable
                  ? exportBlockText(row)
                  : selectedById.size >= MAX_SELECTION_COUNT && !selectedById.has(row.id)
                    ? `최대 ${MAX_SELECTION_COUNT}건까지 선택할 수 있습니다.`
                    : "",
              }),
            }}
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: true,
              pageSizeOptions: [20, 30, 50, 100],
              showTotal: (count) => `총 ${count}건`,
              onChange: (nextPage, nextPageSize) => {
                setPage(nextPageSize === pageSize ? nextPage : 1);
                setPageSize(nextPageSize);
              },
            }}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={filtersApplied ? "조건에 맞는 결과가 없어요. 필터를 바꾸면 다시 찾을 수 있어요." : "지금 검토할 개찰결과가 없어요."}
                >
                  {filtersApplied ? <Button onClick={resetFilters}>필터 초기화</Button> : null}
                </Empty>
              ),
            }}
          />
        )}
      </Card>

      <Card
        className={`opening-results-selection-dock${selectedById.size ? "" : " is-empty"}`}
      >
        <div className="opening-results-selection-row">
          <Space wrap>
            <div className="opening-results-selection-count">{selectedById.size}</div>
            <Typography.Text strong>건 선택</Typography.Text>
            <Button disabled={!selectedById.size} onClick={() => setSelectionOpen(true)}>
              선택 항목 보기
            </Button>
            <Button disabled={!selectedById.size} onClick={clearSelection}>
              전체 해제
            </Button>
          </Space>
          <Space wrap>
            <Select
              value={destinationId}
              onChange={setDestinationId}
              placeholder="Sheet 목적지 선택"
              className="opening-results-destination-select"
              options={usableDestinations.map((item) => ({
                value: item.id,
                label: `${item.label} · ${item.scope === "PERSONAL" ? "개인" : "조직"}`,
              }))}
            />
            {!usableDestinations.length ? (
              <Button type="primary" onClick={openDestinationModal}>
                내 Sheet 연결
              </Button>
            ) : (
              <Button
                type="primary"
                onClick={openPreview}
                loading={previewing}
                disabled={!selectedById.size || !destinationId}
              >
                선택한 {selectedById.size}건 검토
              </Button>
            )}
          </Space>
        </div>
      </Card>

      <Drawer
        title="내 개찰결과 조건 설정"
        width={520}
        open={profileOpen}
        onClose={() => setProfileOpen(false)}
        extra={
          <Button type="primary" loading={savingProfile} onClick={saveProfile}>
            저장
          </Button>
        }
      >
        <Space direction="vertical" size={20} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="포함 키워드 중 하나라도 일치하고 제외 키워드가 없을 때만 표시합니다."
          />
          <Space>
            <Typography.Text strong>내 키워드 매칭 사용</Typography.Text>
            <Switch
              checked={profileEnabled}
              onChange={setProfileEnabled}
            />
          </Space>
          <div>
            <Typography.Text strong>내 포함 키워드 · OR 조건</Typography.Text>
            <Select
              mode="tags"
              value={keywords}
              onChange={setKeywords}
              tokenSeparators={[","]}
              placeholder="AI, 클라우드, 연수"
              style={{ width: "100%", marginTop: 8 }}
            />
          </div>
          <div>
            <Typography.Text strong>내 제외 키워드 · 하나라도 있으면 제외</Typography.Text>
            <Select
              mode="tags"
              value={excludedKeywords}
              onChange={setExcludedKeywords}
              tokenSeparators={[","]}
              placeholder="연수구, 연수원"
              style={{ width: "100%", marginTop: 8 }}
            />
          </div>
          <Typography.Paragraph type="secondary">
            예: 포함 `연수`, 제외 `연수구`, `연수원`이면 연수 사업은 남기고 지역명·기관명 오탐은 제외합니다.
          </Typography.Paragraph>
          <div>
            <Typography.Text strong>Google Sheet 연결</Typography.Text>
            <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
              내가 고른 결과를 반영할 개인 Sheet를 연결하거나, 등록된 연결을 관리합니다.
            </Typography.Paragraph>
            <Button
              onClick={() => {
                setProfileOpen(false);
                openDestinationModal();
              }}
            >
              Sheet 연결 관리
            </Button>
          </div>
        </Space>
      </Drawer>

      <Modal
        title="내 Google Sheet 연결"
        open={destinationOpen}
        width={860}
        footer={null}
        onCancel={closeDestinationModal}
      >
        <Space direction="vertical" size={20} style={{ width: "100%" }}>
          <Alert
            type={settings?.sheet_service_account_email ? "info" : "warning"}
            showIcon
            message={
              settings?.sheet_service_account_email
                ? "먼저 아래 서비스계정을 Google Sheet의 편집자로 공유하세요."
                : "서비스계정 이메일 설정이 없어 연결을 시작할 수 없습니다."
            }
            description={
              settings?.sheet_service_account_email ? (
                <Space wrap>
                  <Typography.Text copyable>{settings.sheet_service_account_email}</Typography.Text>
                  <Button size="small" onClick={copyServiceAccountEmail}>복사</Button>
                </Space>
              ) : (
                "백엔드의 GSHEET_SERVICE_ACCOUNT_EMAIL 설정을 관리자에게 요청하세요."
              )
            }
          />

          <div className="opening-results-destination-form">
            <div>
              <Typography.Text strong>표시 이름</Typography.Text>
              <Input
                value={destinationDraft.label}
                onChange={(event) => changeDestinationDraft("label", event.target.value)}
                placeholder="내 개찰결과 Sheet"
              />
            </div>
            <div>
              <Typography.Text strong>Google Sheet URL 또는 ID</Typography.Text>
              <Input
                value={destinationDraft.spreadsheet_id}
                onChange={(event) => changeDestinationDraft("spreadsheet_id", event.target.value)}
                placeholder="https://docs.google.com/spreadsheets/d/.../edit"
                disabled={verifyingDestination}
              />
            </div>
            <div>
              <Typography.Text strong>탭 이름</Typography.Text>
              <Input
                value={destinationDraft.tab_name}
                onChange={(event) => changeDestinationDraft("tab_name", event.target.value)}
                disabled={verifyingDestination}
              />
            </div>
            <div>
              <Typography.Text strong>사용 범위</Typography.Text>
              <Input value="내 개인 Sheet" disabled />
            </div>
          </div>

          {connectionError ? (
            <Alert type="error" showIcon message="연결 테스트 실패" description={connectionError} />
          ) : null}
          {connectionResult ? (
            <Alert
              type={connectionMeta.type}
              showIcon
              message={connectionResult.connection_ready ? "연결할 수 있습니다." : "연결을 저장할 수 없습니다."}
              description={
                <Space direction="vertical" size={2}>
                  <span>문서: {connectionResult.spreadsheet_title || "제목 없음"}</span>
                  <span>탭: {connectionResult.tab_exists ? `${connectionResult.tab_name} 확인` : `${connectionResult.tab_name} 없음`}</span>
                  <span>헤더: {connectionMeta.text}</span>
                </Space>
              }
            />
          ) : null}

          <div className="opening-results-destination-actions">
            <Button
              onClick={verifyDestination}
              loading={verifyingDestination}
              disabled={!settings?.sheet_service_account_email}
            >
              연결 테스트 · 읽기 전용
            </Button>
            <Button
              type="primary"
              onClick={saveDestination}
              loading={savingDestination}
              disabled={!destinationVerified}
            >
              검증된 연결 저장
            </Button>
          </div>

          <div>
            <Typography.Title level={5}>등록된 Sheet</Typography.Title>
            {usableDestinations.length ? (
              <div className="opening-results-destination-list">
                {usableDestinations.map((destination) => (
                  <div className="opening-results-destination-item" key={destination.id}>
                    <div>
                      <Typography.Text strong>{destination.label}</Typography.Text>
                      <Typography.Paragraph type="secondary">
                        {destination.scope === "PERSONAL" ? "개인" : "조직"} · {destination.tab_name} 탭
                      </Typography.Paragraph>
                    </div>
                    <Space>
                      <Button
                        type={destination.id === destinationId ? "primary" : "default"}
                        disabled={destination.id === destinationId}
                        onClick={() => {
                          setDestinationId(destination.id);
                          closeDestinationModal();
                          message.success("Google Sheet 목적지를 선택했습니다.");
                        }}
                      >
                        {destination.id === destinationId ? "사용 중" : "이 목적지 사용"}
                      </Button>
                      <Button href={sheetUrl(destination.spreadsheet_id)} target="_blank" rel="noreferrer">
                        열기
                      </Button>
                      <Popconfirm
                        title="이 Sheet 연결을 제거할까요?"
                        description="기존 반영 기록과 Sheet 내용은 유지됩니다."
                        okText="제거"
                        cancelText="닫기"
                        onConfirm={() => deleteDestination(destination)}
                        disabled={!canDeleteDestination(destination)}
                      >
                        <Button danger disabled={!canDeleteDestination(destination)}>
                          연결 제거
                        </Button>
                      </Popconfirm>
                    </Space>
                  </div>
                ))}
              </div>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="등록된 Sheet가 없습니다." />
            )}
          </div>
        </Space>
      </Modal>

      <Modal
        title="선택한 결과"
        open={selectionOpen}
        width={760}
        footer={
          <Space>
            <Button onClick={() => setSelectionOpen(false)}>닫기</Button>
            <Button danger disabled={!selectedById.size} onClick={clearSelection}>
              전체 해제
            </Button>
          </Space>
        }
        onCancel={() => setSelectionOpen(false)}
      >
        <Typography.Paragraph type="secondary">
          페이지나 검색 조건을 이동해도 최대 {MAX_SELECTION_COUNT}건까지 이 브라우저 세션에 유지됩니다.
        </Typography.Paragraph>
        <Table
          rowKey="id"
          size="small"
          dataSource={selectedRows}
          columns={selectionColumns}
          pagination={false}
          scroll={{ y: 420 }}
        />
      </Modal>

      <Modal
        title="Google Sheets 반영 전 확인"
        open={Boolean(previewData)}
        width={1280}
        closable={!writing}
        maskClosable={!writing}
        onCancel={() => {
          if (!writing) {
            setPreviewData(null);
            setPreviewTarget(null);
            setPreviewError("");
          }
        }}
        footer={
          <Space>
            <Button
              disabled={writing}
              onClick={() => {
                setPreviewData(null);
                setPreviewTarget(null);
                setPreviewError("");
              }}
            >
              닫기
            </Button>
            <Button type="primary" danger loading={writing} onClick={confirmExport}>
              Google Sheets에 {previewData?.row_count || 0}건 반영
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Alert
            type="warning"
            showIcon
            message="아직 Google Sheets에 반영하지 않았어요."
            description={
              previewData?.destination_scope === "ORGANIZATION"
                ? "조직 공용 Sheet에 반영하면 성공한 결과가 같은 조직 전체의 검토함에서 숨겨집니다. 아래 17개 열과 목적지를 확인한 뒤 최종 반영하세요."
                : "아래 17개 열과 목적지를 확인한 뒤 최종 반영을 눌러야 실제 쓰기가 실행됩니다. 선택하지 않은 Sheet 행은 변경하지 않습니다."
            }
          />
          <Typography.Text strong>
            목적지: {previewData?.destination_label} · {previewData?.destination_tab_name} 탭 · {previewData?.destination_scope === "PERSONAL" ? "개인" : "조직 공용"}
          </Typography.Text>
          {previewError ? (
            <Alert type="error" showIcon message="최종 반영 실패" description={previewError} />
          ) : null}
          <Table
            size="small"
            rowKey="key"
            dataSource={(previewData?.preview_rows || []).map((values, index) => ({ key: index, values }))}
            columns={(previewData?.headers || []).map((header, index) => ({
              title: header,
              key: `${header}-${index}`,
              width: index < 7 ? 145 : 120,
              render: (_, record) => record.values[index] ?? "",
            }))}
            pagination={false}
            scroll={{ x: 2100, y: 420 }}
          />
        </Space>
      </Modal>

      <Drawer
        title={`14일 보관함 · ${archiveTotal.toLocaleString("ko-KR")}건`}
        width={980}
        open={archiveOpen}
        onClose={() => {
          archiveRequestId.current += 1;
          setArchiveOpen(false);
          setArchiveLoading(false);
        }}
        extra={
          <Button onClick={() => loadArchive(archivePage, archivePageSize)} loading={archiveLoading}>
            보관함 새로고침
          </Button>
        }
      >
        <Space direction="vertical" size={16} className="opening-results-archive-content">
          <Alert
            type="info"
            showIcon
            message="제외하거나 Sheet에 반영한 결과를 14일간 보관합니다."
            description="목록 제외 항목은 기간 안에 복구할 수 있습니다. Sheet 반영 항목은 외부 문서 기록이 이미 완료되어 열람만 가능하며, 14일이 지나면 이 보관함에서 자동으로 사라집니다."
          />
          {archiveError ? (
            <Alert
              type="error"
              showIcon
              message="보관함을 불러오지 못했습니다."
              description={archiveError}
              action={<Button onClick={() => loadArchive(archivePage, archivePageSize)}>다시 시도</Button>}
            />
          ) : (
            <Table
              className="opening-results-table-card"
              rowKey="id"
              loading={archiveLoading}
              dataSource={archiveRows}
              columns={archiveColumns}
              pagination={{
                current: archivePage,
                pageSize: archivePageSize,
                total: archiveTotal,
                showSizeChanger: true,
                pageSizeOptions: [20, 30, 50, 100],
                showTotal: (count) => `총 ${count}건`,
                onChange: (nextPage, nextPageSize) => {
                  setArchivePage(nextPageSize === archivePageSize ? nextPage : 1);
                  setArchivePageSize(nextPageSize);
                },
              }}
              locale={{
                emptyText: (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="최근 14일 안에 보관된 결과가 없습니다."
                  />
                ),
              }}
              scroll={{ x: 850 }}
            />
          )}
        </Space>
      </Drawer>

      <Drawer
        title={businessTitle(detail)}
        width={860}
        open={Boolean(detail)}
        loading={detailLoading}
        onClose={() => {
          detailRequestId.current += 1;
          setDetail(null);
          setDetailLoading(false);
        }}
        extra={
          detail?.id && !detail.from_archive ? (
            <Popconfirm
              title="내 검토함에서 제외할까요?"
              description="14일 보관함으로 이동하며, 기간 안에는 다시 복구할 수 있습니다."
              okText="제외"
              cancelText="취소"
              onConfirm={() => dismissRow(detail.id)}
            >
              <Button danger>내 목록에서 제외</Button>
            </Popconfirm>
          ) : detail?.can_restore ? (
            <Popconfirm
              title="검토할 결과로 복구할까요?"
              description="복구하면 보관함에서 빠지고 원래 목록에 다시 표시됩니다."
              okText="복구"
              cancelText="취소"
              onConfirm={() => restoreArchiveRow(detail.id)}
            >
              <Button type="primary" loading={restoringId === detail.id}>검토함으로 복구</Button>
            </Popconfirm>
          ) : null
        }
      >
        {detail ? (
          <Space direction="vertical" size={20} style={{ width: "100%" }}>
            {detail.handled_state ? (
              <Alert
                type={detail.handled_state === "EXPORTED" ? "success" : "info"}
                showIcon
                message={detail.handled_state === "EXPORTED" ? "Google Sheet 반영 완료" : "내 목록에서 제외됨"}
                description={detail.handled_state === "EXPORTED"
                  ? "외부 Sheet 기록이 완료된 항목으로, 보관함에서는 열람만 할 수 있습니다."
                  : `${archiveDaysRemaining(detail.expires_at)}일 안에 검토할 결과로 복구할 수 있습니다.`}
              />
            ) : null}
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="공고번호">{detail.bid_notice_no || "-"}</Descriptions.Item>
              <Descriptions.Item label="개찰상태">
                <Tag className={`opening-status-tag is-${String(detail.status || "unknown").toLowerCase()}`}>
                  {detailStatus.label}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="개찰순위 1위">
                {detailFirstRank ? (
                  <Space direction="vertical" size={2}>
                    <span>{detailFirstRank.company_name || "업체명 미확인"}</span>
                    {formatScoreExpression(detailFirstRank) ? (
                      <Typography.Text type="secondary">
                        {formatScoreExpression(detailFirstRank)}
                      </Typography.Text>
                    ) : null}
                  </Space>
                ) : "순위 미확인"}
              </Descriptions.Item>
              <Descriptions.Item label="최종 낙찰자">
                {detail.winner_company_name || "공식 확인 전"}
              </Descriptions.Item>
              <Descriptions.Item label="수요기관">{detail.demand_agency_name || "-"}</Descriptions.Item>
              <Descriptions.Item label="개찰일">{detail.opened_at ? dayjs(detail.opened_at).format("YYYY-MM-DD HH:mm") : "-"}</Descriptions.Item>
              <Descriptions.Item label="사업금액">{formatMoney(detail.base_amount)}</Descriptions.Item>
              <Descriptions.Item label="제안마감">{detail.proposal_deadline ? dayjs(detail.proposal_deadline).format("YYYY-MM-DD HH:mm") : "-"}</Descriptions.Item>
              <Descriptions.Item label="지역제한">{regionRestrictionText(detail)}</Descriptions.Item>
              <Descriptions.Item label="2단계 입찰">{detail.is_two_stage_bid == null ? "-" : detail.is_two_stage_bid ? "예" : "아니오"}</Descriptions.Item>
              <Descriptions.Item label="참가업체">{detail.participant_count == null ? "-" : `${detail.participant_count}개사`}</Descriptions.Item>
              <Descriptions.Item label="매칭 키워드">{detail.matched_keywords?.length ? detail.matched_keywords.map((keyword) => <Tag className="opening-keyword-tag" key={keyword}>{keyword}</Tag>) : "-"}</Descriptions.Item>
              <Descriptions.Item label="본 공고" span={2}>
                {detailNoticeUrl ? (
                  <Button type="link" href={detailNoticeUrl} target="_blank" rel="noopener noreferrer" className="opening-results-notice-link">
                    나라장터 공고 바로가기
                  </Button>
                ) : (
                  <Typography.Text type="secondary">연결된 공식 공고 링크가 없습니다.</Typography.Text>
                )}
              </Descriptions.Item>
            </Descriptions>
            {!detail.sheet_exportable ? (
              <Alert type="warning" showIcon message="현재 Sheet 반영 불가" description={exportBlockText(detail)} />
            ) : null}
            <div>
              <Typography.Title level={5}>상위 5개 업체 점수</Typography.Title>
              <Table
                rowKey="id"
                size="small"
                dataSource={(detail.entries || []).filter((entry) => entry.rank != null && entry.rank <= 5)}
                columns={rankingColumns}
                pagination={false}
                locale={{
                  emptyText: detailEntriesPending
                    ? "업체별 순위·점수 상세를 수집하고 있습니다."
                    : "공개된 업체별 평가점수가 없습니다.",
                }}
              />
              <Typography.Paragraph type="secondary" className="opening-results-score-note">
                가격점수와 기술점수가 모두 있을 때만 `가격+기술=합계`로 표시하며 합계는 소수점 둘째 자리까지 반올림합니다.
              </Typography.Paragraph>
            </div>
          </Space>
        ) : null}
      </Drawer>
    </div>
  );
}

export default OpeningResultsPage;
