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
import {
  formatApiError,
  preSpecificationsApi,
} from "../api/client";
import "./PreSpecificationsPage.css";

const { RangePicker } = DatePicker;
const MAX_SELECTION_COUNT = 100;

const DEADLINE_META = {
  OPEN: { color: "green", label: "의견 접수 중" },
  TODAY: { color: "gold", label: "오늘 마감" },
  CLOSED: { color: "default", label: "마감" },
  UNKNOWN: { color: "default", label: "마감일 미확인" },
};

const ARCHIVE_STATE_META = {
  DISMISSED: { color: "default", label: "목록 제외" },
  EXPORTED: { color: "blue", label: "Sheet 반영" },
};

const HEADER_STATUS_META = {
  MATCH: { type: "success", text: "A:L 헤더가 올바릅니다." },
  EMPTY: { type: "success", text: "빈 탭입니다. 첫 반영 시 고정 헤더를 만듭니다." },
  MISMATCH: {
    type: "error",
    text: "기존 헤더가 사전규격 12개 열과 다릅니다. 빈 탭이나 올바른 헤더의 탭을 사용하세요.",
  },
  NOT_CHECKED: { type: "warning", text: "탭을 확인하지 못했습니다." },
};

function formatDateTime(value) {
  if (!value) return "-";
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("YYYY.MM.DD HH:mm") : "-";
}

function formatMoney(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString("ko-KR")}원` : String(value);
}

function externalHttpUrl(value) {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.toString() : null;
  } catch {
    return null;
  }
}

function sheetUrl(spreadsheetId) {
  return spreadsheetId
    ? `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit`
    : null;
}

function archiveDaysRemaining(expiresAt) {
  if (!expiresAt) return 0;
  return Math.max(0, Math.ceil(dayjs(expiresAt).diff(dayjs(), "hour", true) / 24));
}

function PreSpecificationsPage() {
  const listRequestId = useRef(0);
  const detailRequestId = useRef(0);
  const archiveRequestId = useRef(0);
  const destinationVerifyRequestId = useRef(0);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(30);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [lastLoadedAt, setLastLoadedAt] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [queryDraft, setQueryDraft] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [registeredRange, setRegisteredRange] = useState(null);
  const [attachmentFilter, setAttachmentFilter] = useState("ALL");
  const [deadlineFilter, setDeadlineFilter] = useState("ALL");
  const [selectedById, setSelectedById] = useState(() => new Map());
  const [selectionOpen, setSelectionOpen] = useState(false);
  const [sheetSettings, setSheetSettings] = useState(null);
  const [sheetSettingsError, setSheetSettingsError] = useState("");
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
    label: "내 사전규격 Sheet",
    spreadsheet_id: "",
    tab_name: "사전규격",
  });
  const [previewing, setPreviewing] = useState(false);
  const [writing, setWriting] = useState(false);
  const [previewData, setPreviewData] = useState(null);
  const [previewTarget, setPreviewTarget] = useState(null);
  const [previewError, setPreviewError] = useState("");
  const [detailOpen, setDetailOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveRows, setArchiveRows] = useState([]);
  const [archiveTotal, setArchiveTotal] = useState(0);
  const [archivePage, setArchivePage] = useState(1);
  const [archivePageSize, setArchivePageSize] = useState(30);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [archiveError, setArchiveError] = useState("");
  const [restoringId, setRestoringId] = useState(null);

  const selectedRows = useMemo(() => [...selectedById.values()], [selectedById]);
  const selectedRowKeys = useMemo(() => [...selectedById.keys()], [selectedById]);
  const usableDestinations = useMemo(
    () => sheetSettings?.sheet_destinations || [],
    [sheetSettings]
  );
  const selectedDestination = useMemo(
    () => usableDestinations.find((item) => item.id === destinationId),
    [destinationId, usableDestinations]
  );

  useEffect(() => {
    const requestId = listRequestId.current + 1;
    listRequestId.current = requestId;
    setLoading(true);
    setListError("");

    const params = {
      page,
      page_size: pageSize,
      attachment: attachmentFilter,
      deadline_status: deadlineFilter,
    };
    if (appliedQuery) params.q = appliedQuery;
    if (registeredRange?.[0]) {
      params.registered_from = registeredRange[0].format("YYYY-MM-DD");
    }
    if (registeredRange?.[1]) {
      params.registered_to = registeredRange[1].format("YYYY-MM-DD");
    }

    preSpecificationsApi
      .list(params)
      .then((response) => {
        if (listRequestId.current !== requestId) return;
        const nextRows = response.data.items || [];
        setRows(nextRows);
        setTotal(response.data.total || 0);
        setLastLoadedAt(dayjs());
        setSelectedById((current) => {
          const next = new Map(current);
          nextRows.forEach((row) => {
            if (next.has(row.bf_spec_rgst_no)) next.set(row.bf_spec_rgst_no, row);
          });
          return next;
        });
      })
      .catch((error) => {
        if (listRequestId.current !== requestId) return;
        setRows([]);
        setTotal(0);
        setListError(formatApiError(error, "사전규격 목록을 불러오지 못했습니다."));
      })
      .finally(() => {
        if (listRequestId.current === requestId) setLoading(false);
      });
  }, [appliedQuery, attachmentFilter, deadlineFilter, page, pageSize, registeredRange, reloadKey]);

  const loadSheetSettings = async () => {
    setSheetSettingsError("");
    try {
      const response = await preSpecificationsApi.settings();
      const nextSettings = response.data;
      const destinations = nextSettings.sheet_destinations || [];
      setSheetSettings(nextSettings);
      setProfileEnabled(nextSettings.profile?.enabled ?? false);
      setKeywords(nextSettings.profile?.keywords || []);
      setExcludedKeywords(nextSettings.profile?.excluded_keywords || []);
      setDestinationId((current) => {
        if (destinations.some((item) => item.id === current)) return current;
        return destinations.find((item) => item.is_default)?.id ?? destinations[0]?.id ?? null;
      });
      return nextSettings;
    } catch (error) {
      setSheetSettingsError(
        formatApiError(error, "사전규격 설정을 불러오지 못했습니다.")
      );
      return null;
    }
  };

  useEffect(() => {
    loadSheetSettings();
  }, []);

  const loadArchive = async (
    nextPage = archivePage,
    nextPageSize = archivePageSize
  ) => {
    const requestId = archiveRequestId.current + 1;
    archiveRequestId.current = requestId;
    setArchiveLoading(true);
    setArchiveError("");
    try {
      const response = await preSpecificationsApi.listArchive({
        page: nextPage,
        page_size: nextPageSize,
      });
      if (archiveRequestId.current !== requestId) return;
      const nextTotal = response.data.total || 0;
      const maxPage = Math.max(1, Math.ceil(nextTotal / nextPageSize));
      setArchiveTotal(nextTotal);
      if (nextPage > maxPage) {
        setArchivePage(maxPage);
        return;
      }
      setArchiveRows(response.data.items || []);
    } catch (error) {
      if (archiveRequestId.current !== requestId) return;
      setArchiveRows([]);
      setArchiveTotal(0);
      setArchiveError(formatApiError(error, "사전규격 14일 보관함을 불러오지 못했습니다."));
    } finally {
      if (archiveRequestId.current === requestId) setArchiveLoading(false);
    }
  };

  useEffect(() => {
    if (archiveOpen) loadArchive(archivePage, archivePageSize);
  }, [archiveOpen, archivePage, archivePageSize]);

  const clearSelection = () => setSelectedById(new Map());

  const manualRefresh = () => {
    if (selectedById.size) {
      clearSelection();
      message.info("목록을 새로 읽어 선택 항목을 비웠습니다.");
    }
    setReloadKey((current) => current + 1);
  };

  const applySearch = () => {
    setPage(1);
    setAppliedQuery(queryDraft.trim());
  };

  const resetFilters = () => {
    setQueryDraft("");
    setAppliedQuery("");
    setRegisteredRange(null);
    setAttachmentFilter("ALL");
    setDeadlineFilter("ALL");
    setPage(1);
  };

  const toggleSelected = (row, checked) => {
    if (
      checked &&
      !selectedById.has(row.bf_spec_rgst_no) &&
      selectedById.size >= MAX_SELECTION_COUNT
    ) {
      message.warning(`한 번에 최대 ${MAX_SELECTION_COUNT}건까지 선택할 수 있습니다.`);
      return;
    }
    setSelectedById((current) => {
      const next = new Map(current);
      if (checked) next.set(row.bf_spec_rgst_no, row);
      else next.delete(row.bf_spec_rgst_no);
      return next;
    });
  };

  const togglePageSelection = (checked, _selectedRows, changedRows) => {
    const next = new Map(selectedById);
    let skipped = 0;
    changedRows.forEach((row) => {
      if (!checked) {
        next.delete(row.bf_spec_rgst_no);
      } else if (next.size < MAX_SELECTION_COUNT) {
        next.set(row.bf_spec_rgst_no, row);
      } else {
        skipped += 1;
      }
    });
    setSelectedById(next);
    if (skipped) {
      message.warning(`${MAX_SELECTION_COUNT}건을 초과한 ${skipped}건은 선택하지 않았습니다.`);
    }
  };

  const openDetail = async (row, fromArchive = false) => {
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    setDetailOpen(true);
    setDetail(null);
    setDetailError("");
    setDetailLoading(true);
    try {
      const response = fromArchive
        ? await preSpecificationsApi.archiveDetail(row.bf_spec_rgst_no)
        : await preSpecificationsApi.detail(row.bf_spec_rgst_no);
      if (detailRequestId.current === requestId) {
        setDetail({ ...response.data, from_archive: fromArchive });
      }
    } catch (error) {
      if (detailRequestId.current === requestId) {
        setDetailError(formatApiError(error, "사전규격 상세를 불러오지 못했습니다."));
      }
    } finally {
      if (detailRequestId.current === requestId) setDetailLoading(false);
    }
  };

  const saveProfile = async () => {
    if (profileEnabled && keywords.length === 0) {
      message.warning("활성화할 때는 포함 키워드를 한 개 이상 입력하세요.");
      return;
    }
    setSavingProfile(true);
    try {
      await preSpecificationsApi.updateProfile({
        enabled: profileEnabled,
        keywords,
        excluded_keywords: excludedKeywords,
      });
      clearSelection();
      setProfileOpen(false);
      setPage(1);
      setReloadKey((current) => current + 1);
      await loadSheetSettings();
      message.success("내 사전규격 키워드 조건을 저장하고 선택을 비웠습니다.");
    } catch (error) {
      message.error(formatApiError(error, "사전규격 조건 저장에 실패했습니다."));
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
      label: "내 사전규격 Sheet",
      spreadsheet_id: "",
      tab_name: "사전규격",
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
      const response = await preSpecificationsApi.verifySheetDestination({
        spreadsheet_id: requestedSpreadsheetId,
        tab_name: requestedTabName,
      });
      if (requestId !== destinationVerifyRequestId.current) return;
      setDestinationDraft((current) => ({
        ...current,
        spreadsheet_id: response.data.spreadsheet_id,
      }));
      setConnectionResult(response.data);
      if (response.data.connection_ready) message.success("읽기 전용 연결 테스트를 통과했습니다.");
    } catch (error) {
      if (requestId !== destinationVerifyRequestId.current) return;
      setConnectionResult(null);
      setConnectionError(
        formatApiError(error, "Google Sheet 연결 확인에 실패했습니다. 공유 권한을 확인하세요.")
      );
    } finally {
      if (requestId === destinationVerifyRequestId.current) setVerifyingDestination(false);
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
      const response = await preSpecificationsApi.saveSheetDestination({
        ...destinationDraft,
        is_default: true,
      });
      await loadSheetSettings();
      setDestinationId(response.data.id);
      closeDestinationModal();
      message.success("검증된 Google Sheet 목적지를 저장했습니다.");
    } catch (error) {
      if (error?.response?.status === 409) {
        const nextSettings = await loadSheetSettings();
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
      await preSpecificationsApi.deleteSheetDestination(target.id);
      await loadSheetSettings();
      message.success("Google Sheet 연결을 목록에서 제거했습니다.");
    } catch (error) {
      message.error(formatApiError(error, "Google Sheet 연결 제거에 실패했습니다."));
    }
  };

  const copyServiceAccountEmail = async () => {
    const email = sheetSettings?.sheet_service_account_email;
    if (!email) return;
    try {
      await navigator.clipboard.writeText(email);
      message.success("서비스계정 이메일을 복사했습니다.");
    } catch {
      message.error("자동 복사에 실패했습니다. 이메일을 직접 선택해 복사하세요.");
    }
  };

  const restoreRow = async (registrationNumber, undoNotificationKey = null) => {
    setRestoringId(registrationNumber);
    try {
      const response = await preSpecificationsApi.restore(registrationNumber);
      if (undoNotificationKey) notification.destroy(undoNotificationKey);
      if (detail?.bf_spec_rgst_no === registrationNumber) {
        detailRequestId.current += 1;
        setDetailOpen(false);
        setDetail(null);
      }
      await loadArchive(archivePage, archivePageSize);
      if (response.data.visible) {
        setReloadKey((current) => current + 1);
        message.success("검토할 사전규격으로 복구했습니다.");
      } else {
        message.warning("Sheet에 반영한 항목은 다시 검토 목록으로 복구할 수 없습니다.");
      }
    } catch (error) {
      message.error(formatApiError(error, "사전규격 복구에 실패했습니다."));
    } finally {
      setRestoringId(null);
    }
  };

  const dismissRow = async (registrationNumber) => {
    try {
      await preSpecificationsApi.dismiss(registrationNumber);
      setSelectedById((current) => {
        const next = new Map(current);
        next.delete(registrationNumber);
        return next;
      });
      if (detail?.bf_spec_rgst_no === registrationNumber) {
        detailRequestId.current += 1;
        setDetailOpen(false);
        setDetail(null);
      }
      setReloadKey((current) => current + 1);
      if (archiveOpen) await loadArchive(archivePage, archivePageSize);
      const notificationKey = `pre-spec-dismiss-${registrationNumber}`;
      notification.open({
        key: notificationKey,
        message: "검토 목록에서 제외했습니다.",
        description: "14일 보관함에서 다시 확인하거나 검토 목록으로 복구할 수 있습니다.",
        duration: 10,
        actions: (
          <Button
            type="link"
            onClick={() => restoreRow(registrationNumber, notificationKey)}
          >
            실행취소
          </Button>
        ),
      });
    } catch (error) {
      message.error(formatApiError(error, "사전규격 제외에 실패했습니다."));
    }
  };

  const openPreview = async () => {
    if (!selectedById.size) {
      message.warning("Google Sheets에 반영할 사전규격을 선택하세요.");
      return;
    }
    if (!destinationId || !selectedDestination) {
      message.warning("먼저 사용할 Google Sheet 목적지를 연결하세요.");
      return;
    }
    const registrationNumbers = [...selectedById.keys()];
    const target = {
      registrationNumbers,
      destinationId,
      url: sheetUrl(selectedDestination.spreadsheet_id),
    };
    setPreviewing(true);
    setPreviewError("");
    try {
      const response = await preSpecificationsApi.exportSheet({
        bf_spec_rgst_nos: registrationNumbers,
        destination_id: destinationId,
        dry_run: true,
      });
      setPreviewData(response.data);
      setPreviewTarget(target);
    } catch (error) {
      message.error(formatApiError(error, "사전규격 Sheet 미리보기에 실패했습니다."));
    } finally {
      setPreviewing(false);
    }
  };

  const closePreview = () => {
    if (writing) return;
    setPreviewData(null);
    setPreviewTarget(null);
    setPreviewError("");
  };

  const confirmExport = async () => {
    if (!previewData || !previewTarget) return;
    setWriting(true);
    setPreviewError("");
    try {
      const response = await preSpecificationsApi.exportSheet({
        bf_spec_rgst_nos: previewTarget.registrationNumbers,
        destination_id: previewTarget.destinationId,
        dry_run: false,
        expected_preview_token: previewData.preview_token,
      });
      const targetUrl = previewTarget.url;
      setPreviewData(null);
      setPreviewTarget(null);
      clearSelection();
      setReloadKey((current) => current + 1);
      if (archiveOpen) await loadArchive(archivePage, archivePageSize);
      notification.success({
        message: "사전규격 Sheet 반영 완료",
        description: `${response.data.inserted_count}건 추가, ${response.data.updated_count}건 갱신했습니다. 반영한 항목은 검토 목록에서 숨겨지고 14일 보관함에 남습니다.`,
        duration: 8,
        actions: targetUrl ? (
          <Button type="primary" href={targetUrl} target="_blank" rel="noreferrer">
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

  const columns = [
    {
      title: "등록일",
      dataIndex: "registered_at",
      width: 150,
      render: formatDateTime,
    },
    {
      title: "등록번호",
      dataIndex: "bf_spec_rgst_no",
      width: 160,
    },
    {
      title: "사업명 / 수요기관",
      dataIndex: "business_name",
      width: 350,
      render: (_, row) => (
        <div className="pre-specification-business">
          <Button type="link" onClick={() => openDetail(row)}>
            {row.business_name || "사업명 미확인"}
          </Button>
          <span>{row.demand_agency_name || "수요기관 미확인"}</span>
        </div>
      ),
    },
    {
      title: "배정예산",
      dataIndex: "allocated_budget",
      width: 150,
      align: "right",
      render: formatMoney,
    },
    {
      title: "의견마감",
      dataIndex: "opinion_deadline",
      width: 180,
      render: (value, row) => {
        const meta = DEADLINE_META[row.deadline_status] || DEADLINE_META.UNKNOWN;
        return (
          <div className="pre-specification-deadline">
            <span>{formatDateTime(value)}</span>
            <Tag color={meta.color}>{meta.label}</Tag>
          </div>
        );
      },
    },
    {
      title: "규격서",
      dataIndex: "attachments",
      width: 80,
      align: "center",
      render: (attachments = []) => (attachments.length ? `${attachments.length}개` : "-"),
    },
    {
      title: "작업",
      key: "actions",
      width: 180,
      fixed: "right",
      render: (_, row) => (
        <Space size={6}>
          <Button size="small" onClick={() => openDetail(row)}>
            상세보기
          </Button>
          <Popconfirm
            title="이 사전규격을 제외할까요?"
            description="14일 보관함에서 복구할 수 있습니다."
            okText="제외"
            cancelText="취소"
            onConfirm={() => dismissRow(row.bf_spec_rgst_no)}
          >
            <Button size="small" danger>
              제외하기
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const archiveColumns = [
    {
      title: "처리 상태",
      dataIndex: "handled_state",
      width: 110,
      render: (value) => {
        const meta = ARCHIVE_STATE_META[value] || ARCHIVE_STATE_META.DISMISSED;
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "사업명 / 수요기관",
      key: "business",
      width: 330,
      render: (_, row) => (
        <div className="pre-specification-business">
          <Button type="link" onClick={() => openDetail(row, true)}>
            {row.business_name || "사업명 미확인"}
          </Button>
          <span>{row.demand_agency_name || "수요기관 미확인"}</span>
        </div>
      ),
    },
    {
      title: "처리일",
      dataIndex: "handled_at",
      width: 150,
      render: formatDateTime,
    },
    {
      title: "남은 기간",
      dataIndex: "expires_at",
      width: 100,
      render: (value) => `${archiveDaysRemaining(value)}일`,
    },
    {
      title: "작업",
      key: "actions",
      width: 170,
      fixed: "right",
      render: (_, row) => (
        <Space size={6}>
          <Button size="small" onClick={() => openDetail(row, true)}>
            상세보기
          </Button>
          {row.can_restore ? (
            <Popconfirm
              title="검토 목록으로 복구할까요?"
              okText="복구"
              cancelText="취소"
              onConfirm={() => restoreRow(row.bf_spec_rgst_no)}
            >
              <Button size="small" loading={restoringId === row.bf_spec_rgst_no}>
                복구
              </Button>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ];

  const attachmentLinks = (detail?.attachments || [])
    .map((attachment, index) => ({
      key: attachment.key || `${detail?.bf_spec_rgst_no}-${index}`,
      label: attachment.label || `규격서 ${index + 1}`,
      url: externalHttpUrl(attachment.url),
    }))
    .filter((attachment) => attachment.url);
  const connectionMeta = connectionResult
    ? HEADER_STATUS_META[connectionResult.header_status] || HEADER_STATUS_META.NOT_CHECKED
    : null;

  return (
    <section className="pre-specifications-page" aria-labelledby="pre-specifications-title">
      <header className="pre-specifications-hero">
        <div>
          <span className="pre-specifications-eyebrow">나라장터 사전규격</span>
          <Typography.Title id="pre-specifications-title" level={2}>
            사전규격을 검토해요
          </Typography.Title>
          <Typography.Paragraph>
            공고 전 공개된 규격을 확인하고 필요한 항목만 Google Sheets에 반영하세요.
          </Typography.Paragraph>
        </div>

        <div className="pre-specifications-hero-actions">
          {lastLoadedAt ? (
            <Typography.Text type="secondary" className="pre-specifications-loaded-at">
              DB 조회 {lastLoadedAt.format("MM.DD HH:mm:ss")}
            </Typography.Text>
          ) : null}
          <Button onClick={() => setArchiveOpen(true)}>14일 보관함</Button>
          <Button onClick={manualRefresh} loading={loading}>
            목록 새로고침
          </Button>
          <Button onClick={openDestinationModal}>Sheet 연결 관리</Button>
          <Button type="primary" ghost onClick={() => setProfileOpen(true)}>
            조건 설정
          </Button>
        </div>
      </header>

      {listError ? <Alert type="error" showIcon message={listError} /> : null}
      {sheetSettingsError ? (
        <Alert
          type="warning"
          showIcon
          message="사전규격 조건·Sheet 설정을 불러오지 못했습니다."
          description={sheetSettingsError}
          action={<Button onClick={loadSheetSettings}>다시 시도</Button>}
        />
      ) : null}

      {sheetSettings && !usableDestinations.length ? (
        <Alert
          type="warning"
          showIcon
          message="연결된 Google Sheet가 없습니다."
          description="서비스계정을 내 Sheet의 편집자로 공유하고 연결 테스트를 통과하면 저장할 수 있습니다."
          action={<Button type="primary" onClick={openDestinationModal}>내 Sheet 연결</Button>}
        />
      ) : null}

      <Card className="pre-specifications-filter-card">
        <div className="pre-specifications-filter-heading">
          <div>
            <strong>사전규격 찾기</strong>
            <span>등록번호, 사업명, 수요기관으로 검색할 수 있어요.</span>
          </div>
          <span>총 {total.toLocaleString("ko-KR")}건</span>
        </div>
        <div className="pre-specifications-filter-row">
          <Input.Search
            className="pre-specifications-search"
            value={queryDraft}
            onChange={(event) => setQueryDraft(event.target.value)}
            onSearch={applySearch}
            enterButton="검색"
            placeholder="등록번호, 사업명, 수요기관 검색"
            allowClear
          />
          <RangePicker
            className="pre-specifications-date-filter"
            value={registeredRange}
            onChange={(value) => {
              setRegisteredRange(value);
              setPage(1);
            }}
            placeholder={["등록 시작일", "등록 종료일"]}
          />
          <Select
            className="pre-specifications-deadline-filter"
            value={deadlineFilter}
            onChange={(value) => {
              setDeadlineFilter(value);
              setPage(1);
            }}
            options={[
              { value: "ALL", label: "의견마감 전체" },
              { value: "OPEN", label: "의견 접수 중" },
              { value: "TODAY", label: "오늘 마감" },
              { value: "CLOSED", label: "마감" },
              { value: "UNKNOWN", label: "마감일 미확인" },
            ]}
          />
          <Select
            className="pre-specifications-attachment-filter"
            value={attachmentFilter}
            onChange={(value) => {
              setAttachmentFilter(value);
              setPage(1);
            }}
            options={[
              { value: "ALL", label: "규격서 전체" },
              { value: "HAS", label: "규격서 있음" },
              { value: "NONE", label: "규격서 없음" },
            ]}
          />
          <Button onClick={resetFilters}>필터 초기화</Button>
        </div>
        <div className="pre-specifications-condition-row">
          <Space wrap>
            <Typography.Text strong>내 포함 키워드</Typography.Text>
            {sheetSettings?.profile?.enabled ? (
              sheetSettings.profile.keywords?.map((keyword) => <Tag key={keyword}>{keyword}</Tag>)
            ) : (
              <Tag>사용 안 함</Tag>
            )}
            {sheetSettings?.profile?.excluded_keywords?.length ? (
              <>
                <Typography.Text strong>제외</Typography.Text>
                {sheetSettings.profile.excluded_keywords.map((keyword) => (
                  <Tag color="orange" key={keyword}>{keyword}</Tag>
                ))}
              </>
            ) : null}
          </Space>
          <Button type="link" onClick={() => setProfileOpen(true)}>조건 설정</Button>
        </div>
      </Card>

      <Card className="pre-specifications-table-card" title="검토할 사전규격">
        <Table
          rowKey="bf_spec_rgst_no"
          columns={columns}
          dataSource={rows}
          loading={loading}
          rowClassName={(row) =>
            selectedById.has(row.bf_spec_rgst_no) ? "pre-specification-row-selected" : ""
          }
          rowSelection={{
            columnWidth: 44,
            preserveSelectedRowKeys: true,
            selectedRowKeys,
            onSelect: toggleSelected,
            onSelectAll: togglePageSelection,
            getCheckboxProps: (row) => ({
              disabled:
                selectedById.size >= MAX_SELECTION_COUNT &&
                !selectedById.has(row.bf_spec_rgst_no),
              title:
                selectedById.size >= MAX_SELECTION_COUNT &&
                !selectedById.has(row.bf_spec_rgst_no)
                  ? `최대 ${MAX_SELECTION_COUNT}건까지 선택할 수 있습니다.`
                  : "",
            }),
          }}
          scroll={{ x: 1260 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [10, 30, 50, 100],
            showTotal: (count) => `총 ${count.toLocaleString("ko-KR")}건`,
            onChange: (nextPage, nextPageSize) => {
              setPage(nextPageSize === pageSize ? nextPage : 1);
              setPageSize(nextPageSize);
            },
          }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="조건에 맞는 사전규격이 없습니다."
              />
            ),
          }}
        />
      </Card>

      <Card
        className={`pre-specifications-selection-dock${selectedById.size ? "" : " is-empty"}`}
      >
        <div className="pre-specifications-selection-row">
          <Space wrap>
            <div className="pre-specifications-selection-count">{selectedById.size}</div>
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
              className="pre-specifications-destination-select"
              options={usableDestinations.map((item) => ({
                value: item.id,
                label: `${item.label} · 개인`,
              }))}
            />
            {usableDestinations.length ? (
              <Button
                type="primary"
                onClick={openPreview}
                loading={previewing}
                disabled={!selectedById.size || !destinationId}
              >
                선택한 {selectedById.size}건 검토
              </Button>
            ) : (
              <Button type="primary" onClick={openDestinationModal}>
                내 Sheet 연결
              </Button>
            )}
          </Space>
        </div>
      </Card>

      <Drawer
        title="내 사전규격 조건 설정"
        width={520}
        open={profileOpen}
        onClose={() => setProfileOpen(false)}
        extra={<Button type="primary" loading={savingProfile} onClick={saveProfile}>저장</Button>}
      >
        <Space direction="vertical" size={20} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="포함 키워드 중 하나라도 일치하고 제외 키워드가 없을 때만 표시합니다."
          />
          <Space>
            <Typography.Text strong>내 키워드 매칭 사용</Typography.Text>
            <Switch checked={profileEnabled} onChange={setProfileEnabled} />
          </Space>
          <div>
            <Typography.Text strong>내 포함 키워드 · OR 조건</Typography.Text>
            <Select
              mode="tags"
              value={keywords}
              onChange={setKeywords}
              tokenSeparators={[","]}
              placeholder="AI, 클라우드, 교육"
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
            예: 포함 `교육`, 제외 `연수원`이면 교육 사업은 남기고 기관명 오탐은 제외합니다.
          </Typography.Paragraph>
          <div>
            <Typography.Text strong>Google Sheet 연결</Typography.Text>
            <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
              내가 고른 사전규격을 반영할 개인 Sheet를 연결하거나 등록된 연결을 관리합니다.
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
        title="내 사전규격 Google Sheet 연결"
        open={destinationOpen}
        width={860}
        footer={null}
        onCancel={closeDestinationModal}
      >
        <Space direction="vertical" size={20} style={{ width: "100%" }}>
          <Alert
            type={sheetSettings?.sheet_service_account_email ? "info" : "warning"}
            showIcon
            message={
              sheetSettings?.sheet_service_account_email
                ? "먼저 아래 서비스계정을 Google Sheet의 편집자로 공유하세요."
                : "서비스계정 이메일 설정이 없어 연결을 시작할 수 없습니다."
            }
            description={
              sheetSettings?.sheet_service_account_email ? (
                <Space wrap>
                  <Typography.Text copyable>{sheetSettings.sheet_service_account_email}</Typography.Text>
                  <Button size="small" onClick={copyServiceAccountEmail}>복사</Button>
                </Space>
              ) : (
                "백엔드의 GSHEET_SERVICE_ACCOUNT_EMAIL 설정을 관리자에게 요청하세요."
              )
            }
          />

          <div className="pre-specifications-destination-form">
            <div>
              <Typography.Text strong>표시 이름</Typography.Text>
              <Input
                value={destinationDraft.label}
                onChange={(event) => changeDestinationDraft("label", event.target.value)}
                placeholder="내 사전규격 Sheet"
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

          <div className="pre-specifications-destination-actions">
            <Button
              onClick={verifyDestination}
              loading={verifyingDestination}
              disabled={!sheetSettings?.sheet_service_account_email}
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
            <Typography.Title level={5}>등록된 내 Sheet</Typography.Title>
            {usableDestinations.length ? (
              <div className="pre-specifications-destination-list">
                {usableDestinations.map((destination) => (
                  <div className="pre-specifications-destination-item" key={destination.id}>
                    <div>
                      <Typography.Text strong>{destination.label}</Typography.Text>
                      <Typography.Paragraph type="secondary">
                        개인 · {destination.tab_name} 탭
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
                      >
                        <Button danger>연결 제거</Button>
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
        title="선택한 사전규격"
        open={selectionOpen}
        width={760}
        onCancel={() => setSelectionOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setSelectionOpen(false)}>닫기</Button>
            <Button danger disabled={!selectedById.size} onClick={clearSelection}>
              전체 해제
            </Button>
          </Space>
        }
      >
        <Typography.Paragraph type="secondary">
          페이지나 검색 조건을 이동해도 최대 {MAX_SELECTION_COUNT}건까지 유지됩니다.
        </Typography.Paragraph>
        <Table
          rowKey="bf_spec_rgst_no"
          size="small"
          dataSource={selectedRows}
          pagination={false}
          scroll={{ y: 420 }}
          columns={[
            { title: "등록번호", dataIndex: "bf_spec_rgst_no", width: 180 },
            {
              title: "사업명",
              dataIndex: "business_name",
              render: (value) => value || "사업명 미확인",
            },
            {
              title: "제거",
              key: "remove",
              width: 80,
              render: (_, row) => (
                <Button
                  type="link"
                  danger
                  onClick={() => toggleSelected(row, false)}
                >
                  제거
                </Button>
              ),
            },
          ]}
        />
      </Modal>

      <Modal
        title="Google Sheets 반영 전 확인"
        open={Boolean(previewData)}
        width={1280}
        closable={!writing}
        maskClosable={!writing}
        onCancel={closePreview}
        footer={
          <Space>
            <Button disabled={writing} onClick={closePreview}>
              닫기
            </Button>
            <Button type="primary" danger loading={writing} onClick={confirmExport}>
              Google Sheets에 {previewData?.row_count || 0}건 반영
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={16} className="pre-specifications-preview-content">
          <Alert
            type="warning"
            showIcon
            message="아직 Google Sheets에 반영하지 않았어요."
            description={
              "아래 12개 열과 개인 Sheet 목적지를 확인한 뒤 최종 반영을 눌러야 실제 쓰기가 실행됩니다. 선택하지 않은 Sheet 행은 변경하지 않습니다."
            }
          />
          <Typography.Text strong>
            목적지: {previewData?.destination_label} · {previewData?.destination_tab_name} 탭 · 개인
          </Typography.Text>
          {previewError ? (
            <Alert type="error" showIcon message="최종 반영 실패" description={previewError} />
          ) : null}
          <Table
            size="small"
            rowKey="key"
            dataSource={(previewData?.preview_rows || []).map((values, index) => ({
              key: index,
              values,
            }))}
            columns={(previewData?.headers || []).map((header, index) => ({
              title: header,
              key: `${header}-${index}`,
              width: 145,
              render: (_, record) => record.values[index] ?? "",
            }))}
            pagination={false}
            scroll={{ x: 1740, y: 420 }}
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
        <Space direction="vertical" size={16} className="pre-specifications-archive-content">
          <Alert
            type="info"
            showIcon
            message="제외하거나 Sheet에 반영한 사전규격을 14일간 보관합니다."
            description="목록 제외 항목은 기간 안에 복구할 수 있습니다. Sheet 반영 항목은 외부 기록이 완료되어 열람만 가능하며 14일 뒤 자동으로 사라집니다."
          />
          {archiveError ? (
            <Alert
              type="error"
              showIcon
              message="보관함을 불러오지 못했습니다."
              description={archiveError}
              action={<Button onClick={() => loadArchive()}>다시 시도</Button>}
            />
          ) : (
            <Table
              rowKey="bf_spec_rgst_no"
              loading={archiveLoading}
              dataSource={archiveRows}
              columns={archiveColumns}
              scroll={{ x: 860 }}
              pagination={{
                current: archivePage,
                pageSize: archivePageSize,
                total: archiveTotal,
                showSizeChanger: true,
                pageSizeOptions: [10, 30, 50, 100],
                showTotal: (count) => `총 ${count.toLocaleString("ko-KR")}건`,
                onChange: (nextPage, nextPageSize) => {
                  setArchivePage(nextPageSize === archivePageSize ? nextPage : 1);
                  setArchivePageSize(nextPageSize);
                },
              }}
              locale={{
                emptyText: (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="14일 안에 처리한 사전규격이 없습니다."
                  />
                ),
              }}
            />
          )}
        </Space>
      </Drawer>

      <Drawer
        title="사전규격 상세"
        width={640}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        extra={
          detail?.from_archive && detail.can_restore ? (
            <Popconfirm
              title="검토 목록으로 복구할까요?"
              okText="복구"
              cancelText="취소"
              onConfirm={() => restoreRow(detail.bf_spec_rgst_no)}
            >
              <Button loading={restoringId === detail.bf_spec_rgst_no}>복구</Button>
            </Popconfirm>
          ) : detail && !detail.from_archive ? (
            <Popconfirm
              title="이 사전규격을 제외할까요?"
              description="14일 보관함에서 복구할 수 있습니다."
              okText="제외"
              cancelText="취소"
              onConfirm={() => dismissRow(detail.bf_spec_rgst_no)}
            >
              <Button danger>제외하기</Button>
            </Popconfirm>
          ) : null
        }
      >
        {detailError ? <Alert type="error" showIcon message={detailError} /> : null}
        {detailLoading ? (
          <div className="pre-specifications-detail-loading">상세 내용을 불러오고 있습니다.</div>
        ) : null}
        {detail ? (
          <div className="pre-specifications-detail">
            {detail.from_archive ? (
              <Alert
                type={detail.can_restore ? "info" : "success"}
                showIcon
                message={
                  detail.can_restore
                    ? `목록 제외 · ${archiveDaysRemaining(detail.expires_at)}일 남음`
                    : `Sheet 반영 완료 · ${archiveDaysRemaining(detail.expires_at)}일 남음`
                }
              />
            ) : null}
            <div className="pre-specifications-detail-heading">
              <Tag color={(DEADLINE_META[detail.deadline_status] || DEADLINE_META.UNKNOWN).color}>
                {(DEADLINE_META[detail.deadline_status] || DEADLINE_META.UNKNOWN).label}
              </Tag>
              <Typography.Title level={3}>
                {detail.business_name || "사업명 미확인"}
              </Typography.Title>
              <Typography.Text type="secondary">
                {detail.demand_agency_name || "수요기관 미확인"}
              </Typography.Text>
            </div>

            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label="사전규격 등록번호">
                {detail.bf_spec_rgst_no}
              </Descriptions.Item>
              <Descriptions.Item label="참조번호">{detail.reference_no || "-"}</Descriptions.Item>
              <Descriptions.Item label="사업구분">{detail.business_type || "-"}</Descriptions.Item>
              <Descriptions.Item label="공고기관">
                {detail.ordering_agency_name || "-"}
              </Descriptions.Item>
              <Descriptions.Item label="배정예산">
                {formatMoney(detail.allocated_budget)}
              </Descriptions.Item>
              <Descriptions.Item label="등록일">
                {formatDateTime(detail.registered_at)}
              </Descriptions.Item>
              <Descriptions.Item label="의견마감">
                {formatDateTime(detail.opinion_deadline)}
              </Descriptions.Item>
              <Descriptions.Item label="납품기한">
                {formatDateTime(detail.delivery_deadline)}
              </Descriptions.Item>
              <Descriptions.Item label="연결 공고">
                {detail.bid_notice_no
                  ? `${detail.bid_notice_no} (${detail.bid_notice_ord || "차수 미확인"})`
                  : "-"}
              </Descriptions.Item>
              <Descriptions.Item label="담당자">
                {[detail.contact_name, detail.contact_phone].filter(Boolean).join(" · ") || "-"}
              </Descriptions.Item>
            </Descriptions>

            <Card size="small" title={`규격서 첨부 ${attachmentLinks.length}개`}>
              {attachmentLinks.length ? (
                <Space direction="vertical" size={8}>
                  {attachmentLinks.map((attachment) => (
                    <Button
                      key={attachment.key}
                      type="link"
                      href={attachment.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {attachment.label} 바로가기
                    </Button>
                  ))}
                </Space>
              ) : (
                <Typography.Text type="secondary">
                  제공된 규격서 링크가 없습니다.
                </Typography.Text>
              )}
            </Card>
          </div>
        ) : null}
      </Drawer>
    </section>
  );
}

export default PreSpecificationsPage;
