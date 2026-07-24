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
import { bidNoticesApi, formatApiError } from "../api/client";
import "./BidNoticesPage.css";

const { Text, Title } = Typography;
const { RangePicker } = DatePicker;
const MAX_SELECTION_COUNT = 100;

const HEADER_STATUS_META = {
  MATCH: { type: "success", text: "A:L 헤더가 올바릅니다." },
  EMPTY: { type: "success", text: "빈 탭입니다. 첫 반영 시 고정 헤더를 만듭니다." },
  MISMATCH: {
    type: "error",
    text: "기존 헤더가 입찰공고 12개 열과 다릅니다. 빈 탭이나 올바른 헤더의 탭을 사용하세요.",
  },
  NOT_CHECKED: { type: "warning", text: "탭을 확인하지 못했습니다." },
};

const WORK_TYPE_OPTIONS = [
  { value: "공사", label: "공사" },
  { value: "기술용역", label: "기술용역" },
  { value: "물품", label: "물품" },
  { value: "민간일반용역", label: "민간일반용역" },
  { value: "일반용역", label: "일반용역" },
];

const REGION_OPTIONS = [
  "전국",
  "서울특별시",
  "부산광역시",
  "대구광역시",
  "인천광역시",
  "광주광역시",
  "대전광역시",
  "울산광역시",
  "세종특별자치시",
  "경기도",
  "강원특별자치도",
  "충청북도",
  "충청남도",
  "전북특별자치도",
  "전라남도",
  "경상북도",
  "경상남도",
  "제주특별자치도",
].map((value) => ({ value, label: value }));

function formatDateTime(value) {
  if (!value) return "-";
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("YYYY.MM.DD HH:mm") : "-";
}

function formatAmount(value) {
  if (value == null || value === "") return "확인 필요";
  const amount = Number(value);
  return Number.isFinite(amount) ? `${amount.toLocaleString("ko-KR")}원` : String(value);
}

function formatWorkType(value) {
  if (value === "용역") return "일반용역";
  return value || "분류 확인 중";
}

function externalUrl(value) {
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

function BidNoticesPage() {
  const requestId = useRef(0);
  const detailRequestId = useRef(0);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(30);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [workTypes, setWorkTypes] = useState([]);
  const [region, setRegion] = useState();
  const [publishedRange, setPublishedRange] = useState(null);
  const [icoreCodesOnly, setIcoreCodesOnly] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileLoading, setProfileLoading] = useState(false);
  const [savingProfile, setSavingProfile] = useState(false);
  const [profileEnabled, setProfileEnabled] = useState(false);
  const [keywords, setKeywords] = useState([]);
  const [excludedKeywords, setExcludedKeywords] = useState([]);
  const [destinations, setDestinations] = useState([]);
  const [destinationId, setDestinationId] = useState();
  const [connectOpen, setConnectOpen] = useState(false);
  const [sheetServiceAccountEmail, setSheetServiceAccountEmail] = useState("");
  const [verifyingDestination, setVerifyingDestination] = useState(false);
  const [savingDestination, setSavingDestination] = useState(false);
  const [connectionResult, setConnectionResult] = useState(null);
  const [connectionError, setConnectionError] = useState("");
  const [destinationDraft, setDestinationDraft] = useState({
    label: "내 입찰공고 Sheet",
    spreadsheet_id: "",
    tab_name: "입찰공고",
  });
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveRows, setArchiveRows] = useState([]);
  const [archiveTotal, setArchiveTotal] = useState(0);
  const [archivePage, setArchivePage] = useState(1);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [restoringId, setRestoringId] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [selectedById, setSelectedById] = useState(() => new Map());
  const [previewing, setPreviewing] = useState(false);
  const [writing, setWriting] = useState(false);
  const [previewData, setPreviewData] = useState(null);
  const [previewTarget, setPreviewTarget] = useState(null);
  const [previewError, setPreviewError] = useState("");

  const connectionMeta = connectionResult
    ? HEADER_STATUS_META[connectionResult.header_status] || HEADER_STATUS_META.NOT_CHECKED
    : null;
  const selectedRowKeys = useMemo(() => [...selectedById.keys()], [selectedById]);
  const selectedDestination = useMemo(
    () => destinations.find((item) => item.id === destinationId),
    [destinationId, destinations]
  );

  const loadList = () => {
    const nextRequestId = requestId.current + 1;
    requestId.current = nextRequestId;
    setLoading(true);
    setListError("");
    bidNoticesApi
      .list({
        page,
        page_size: pageSize,
        q: query || undefined,
        work_type: workTypes.join(",") || undefined,
        region,
        published_from: publishedRange?.[0]?.format("YYYY-MM-DD"),
        published_to: publishedRange?.[1]?.format("YYYY-MM-DD"),
        icore_codes_only: icoreCodesOnly || undefined,
      })
      .then((response) => {
        if (requestId.current !== nextRequestId) return;
        setRows(response.data.items || []);
        setTotal(response.data.total || 0);
      })
      .catch((error) => {
        if (requestId.current !== nextRequestId) return;
        setRows([]);
        setTotal(0);
        setListError(formatApiError(error, "입찰공고 목록을 불러오지 못했습니다."));
      })
      .finally(() => {
        if (requestId.current === nextRequestId) setLoading(false);
      });
  };

  const loadProfile = async () => {
    setProfileLoading(true);
    try {
      const response = await bidNoticesApi.settings();
      const profile = response.data.profile || {};
      setProfileEnabled(profile.enabled ?? false);
      setKeywords(profile.keywords || []);
      setExcludedKeywords(profile.excluded_keywords || []);
      const nextDestinations = response.data.sheet_destinations || [];
      setDestinations(nextDestinations);
      setDestinationId((current) => current || nextDestinations[0]?.id);
      setSheetServiceAccountEmail(response.data.sheet_service_account_email || "");
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 조건을 불러오지 못했습니다."));
    } finally {
      setProfileLoading(false);
    }
  };

  useEffect(() => {
    loadList();
  }, [page, pageSize, query, workTypes, region, publishedRange, icoreCodesOnly]);

  useEffect(() => {
    loadProfile();
  }, []);

  useEffect(() => {
    if (archiveOpen) loadArchive(archivePage);
  }, [archiveOpen, archivePage]);

  const saveProfile = async () => {
    if (profileEnabled && !keywords.length) {
      message.warning("조건을 켜려면 포함 키워드를 하나 이상 입력하세요.");
      return;
    }
    setSavingProfile(true);
    try {
      const response = await bidNoticesApi.updateProfile({
        enabled: profileEnabled,
        keywords,
        excluded_keywords: excludedKeywords,
      });
      setProfileEnabled(response.data.enabled);
      setKeywords(response.data.keywords || []);
      setExcludedKeywords(response.data.excluded_keywords || []);
      setProfileOpen(false);
      setPage(1);
      setSelectedById(new Map());
      message.success("입찰공고 조건을 저장했습니다.");
      loadList();
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 조건 저장에 실패했습니다."));
    } finally {
      setSavingProfile(false);
    }
  };

  const refreshReview = async () => {
    setSelectedById(new Map());
    await Promise.all([loadList(), loadProfile()]);
    message.success("내 검토 목록을 새로고침했습니다.");
  };

  const loadArchive = async (nextPage = archivePage) => {
    setArchiveLoading(true);
    try {
      const response = await bidNoticesApi.listArchive({ page: nextPage, page_size: pageSize });
      setArchiveRows(response.data.items || []);
      setArchiveTotal(response.data.total || 0);
    } catch (error) {
      message.error(formatApiError(error, "14일 보관함을 불러오지 못했습니다."));
    } finally {
      setArchiveLoading(false);
    }
  };

  const dismissNotice = async (noticeId) => {
    try {
      await bidNoticesApi.dismiss(noticeId);
      setRows((current) => current.filter((row) => row.id !== noticeId));
      setTotal((current) => Math.max(0, current - 1));
      if (detail?.id === noticeId) closeDetail();
      message.success("내 검토함에서 제외했습니다. 14일 보관함에서 복구할 수 있습니다.");
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 제외에 실패했습니다."));
    }
  };

  const restoreNotice = async (noticeId) => {
    setRestoringId(noticeId);
    try {
      await bidNoticesApi.restore(noticeId);
      await Promise.all([loadArchive(archivePage), loadList()]);
      if (detail?.id === noticeId) closeDetail();
      message.success("검토 목록으로 복구했습니다.");
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 복구에 실패했습니다."));
    } finally {
      setRestoringId(null);
    }
  };

  const changeDestinationDraft = (field, value) => {
    setDestinationDraft((draft) => ({ ...draft, [field]: value }));
    setConnectionResult(null);
    setConnectionError("");
  };

  const verifyDestination = async () => {
    if (!destinationDraft.spreadsheet_id || !destinationDraft.tab_name) {
      message.warning("Google Sheet URL 또는 ID와 탭 이름을 입력하세요.");
      return;
    }
    setVerifyingDestination(true);
    setConnectionResult(null);
    setConnectionError("");
    try {
      const verification = await bidNoticesApi.verifySheetDestination(destinationDraft);
      setConnectionResult(verification.data);
    } catch (error) {
      setConnectionError(formatApiError(error, "Google Sheet 연결 테스트에 실패했습니다."));
    } finally {
      setVerifyingDestination(false);
    }
  };

  const saveDestination = async () => {
    if (!connectionResult?.connection_ready) {
      message.warning("먼저 연결 테스트를 통과하세요.");
      return;
    }
    setSavingDestination(true);
    try {
      const saved = await bidNoticesApi.saveSheetDestination(destinationDraft);
      setDestinationId(saved.data.id);
      setConnectionResult(null);
      setConnectionError("");
      message.success("검증된 개인 Google Sheet 연결을 저장했습니다.");
      await loadProfile();
    } catch (error) {
      setConnectionError(formatApiError(error, "Google Sheet 연결 저장에 실패했습니다."));
    } finally {
      setSavingDestination(false);
    }
  };

  const deleteDestination = async (target) => {
    try {
      await bidNoticesApi.deleteSheetDestination(target.id);
      setDestinationId((current) => (current === target.id ? undefined : current));
      await loadProfile();
      message.success("개인 Google Sheet 연결을 제거했습니다.");
    } catch (error) {
      message.error(formatApiError(error, "Google Sheet 연결 제거에 실패했습니다."));
    }
  };

  const openDestinationManager = async () => {
    await loadProfile();
    setConnectOpen(true);
  };

  const copyServiceAccountEmail = async () => {
    try {
      await navigator.clipboard.writeText(sheetServiceAccountEmail);
      message.success("서비스계정 이메일을 복사했습니다.");
    } catch {
      message.warning("서비스계정 이메일 복사에 실패했습니다.");
    }
  };

  const openDetail = async (row, fromArchive = false) => {
    const nextRequestId = detailRequestId.current + 1;
    detailRequestId.current = nextRequestId;
    setDetailOpen(true);
    setDetail({ ...row, from_archive: fromArchive });
    setDetailLoading(true);
    setDetailError("");
    try {
      const response = await bidNoticesApi.detail(row.id);
      if (detailRequestId.current === nextRequestId) {
        setDetail({ ...response.data, from_archive: fromArchive });
      }
    } catch (error) {
      if (detailRequestId.current === nextRequestId) {
        setDetailError(formatApiError(error, "입찰공고 상세를 불러오지 못했습니다."));
      }
    } finally {
      if (detailRequestId.current === nextRequestId) setDetailLoading(false);
    }
  };

  const closeDetail = () => {
    detailRequestId.current += 1;
    setDetailOpen(false);
    setDetail(null);
    setDetailError("");
  };

  const clearSelection = () => setSelectedById(new Map());

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

  const togglePageSelection = (checked, _selectedRows, changedRows) => {
    const next = new Map(selectedById);
    let skipped = 0;
    changedRows.forEach((row) => {
      if (!checked) next.delete(row.id);
      else if (next.size < MAX_SELECTION_COUNT) next.set(row.id, row);
      else skipped += 1;
    });
    setSelectedById(next);
    if (skipped) message.warning(`${MAX_SELECTION_COUNT}건을 초과한 ${skipped}건은 선택하지 않았습니다.`);
  };

  const openExportPreview = async () => {
    if (!selectedById.size) {
      message.warning("Google Sheet에 반영할 입찰공고를 선택하세요.");
      return;
    }
    if (!selectedDestination) {
      message.warning("먼저 사용할 Google Sheet 목적지를 선택하세요.");
      return;
    }
    const target = {
      destinationId: selectedDestination.id,
      noticeIds: [...selectedById.keys()],
      url: sheetUrl(selectedDestination.spreadsheet_id),
    };
    setPreviewing(true);
    setPreviewError("");
    try {
      const response = await bidNoticesApi.exportSheet({
        destination_id: target.destinationId,
        notice_ids: target.noticeIds,
        dry_run: true,
      });
      setPreviewData(response.data);
      setPreviewTarget(target);
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 Sheet 미리보기에 실패했습니다."));
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
      const response = await bidNoticesApi.exportSheet({
        destination_id: previewTarget.destinationId,
        notice_ids: previewTarget.noticeIds,
        dry_run: false,
        expected_preview_token: previewData.preview_token,
      });
      const targetUrl = previewTarget.url;
      setPreviewData(null);
      setPreviewTarget(null);
      setPreviewError("");
      clearSelection();
      notification.success({
        message: "입찰공고 Sheet 반영 완료",
        description: `${response.data.inserted_count}건 추가, ${response.data.updated_count}건 갱신했습니다.`,
        duration: 8,
        actions: targetUrl ? <Button type="primary" href={targetUrl} target="_blank" rel="noreferrer">Sheet 열기</Button> : null,
      });
    } catch (error) {
      setPreviewError(formatApiError(error, "Google Sheet 반영에 실패했습니다. 선택은 유지됩니다."));
    } finally {
      setWriting(false);
    }
  };

  const columns = [
    { title: "게시일", dataIndex: "published_at", key: "published_at", width: 145, render: formatDateTime },
    {
      title: "공고번호",
      key: "notice_no",
      width: 175,
      render: (_, row) => `${row.bid_notice_no || "-"}-${row.bid_notice_ord || "00"}`,
    },
    {
      title: "공고명",
      dataIndex: "business_name",
      key: "business_name",
      width: 340,
      render: (value, row) => {
        return (
          <div className="bid-notice-title-cell">
            <Button type="link" className="bid-notice-detail-button" onClick={() => openDetail(row)}>{value || "공고명 미확인"}</Button>
            <span>{row.demand_agency_name || "수요기관 미확인"}</span>
          </div>
        );
      },
    },
    { title: "업무", dataIndex: "work_type", key: "work_type", width: 116, render: formatWorkType },
    { title: "사업금액", dataIndex: "business_amount", key: "business_amount", width: 145, render: formatAmount },
    {
      title: "매칭",
      dataIndex: "matched_keyword",
      key: "matched_keyword",
      width: 110,
      render: (value) => value ? <Tag color="blue">{value}</Tag> : "-",
    },
    { title: "지역", dataIndex: "region_restriction", key: "region_restriction", width: 140, render: (value) => value || "확인 필요" },
    {
      title: "작업",
      key: "actions",
      width: 86,
      render: (_, row) => (
        <Popconfirm title="이 공고를 검토함에서 제외할까요?" onConfirm={() => dismissNotice(row.id)} okText="제외" cancelText="취소">
          <Button type="link" danger>제외</Button>
        </Popconfirm>
      ),
    },
  ];

  const archiveColumns = [
    ...columns.slice(0, 2),
    {
      ...columns[2],
      render: (value, row) => (
        <div className="bid-notice-title-cell">
          <Button type="link" className="bid-notice-detail-button" onClick={() => openDetail(row, true)}>{value || "공고명 미확인"}</Button>
          <span>{row.demand_agency_name || "수요기관 미확인"}</span>
        </div>
      ),
    },
    ...columns.slice(3, -1),
    {
      title: "작업",
      key: "restore",
      width: 92,
      render: (_, row) => <Button type="link" loading={restoringId === row.id} onClick={() => restoreNotice(row.id)}>복구</Button>,
    },
  ];

  return (
    <div className="bid-notices-page">
      <section className="bid-notices-hero">
        <div>
          <span className="bid-notices-eyebrow">G2B 입찰공고</span>
          <Title level={2}>입찰공고를 검토해요</Title>
          <Text>공통 원본에서 내 포함·제외 키워드에 맞는 공고만 확인합니다.</Text>
        </div>
        <Space wrap>
          <Button onClick={() => setArchiveOpen(true)}>14일 보관함</Button>
          <Button onClick={refreshReview} loading={loading}>목록 새로고침</Button>
          <Button onClick={openDestinationManager}>Sheet 연결 관리</Button>
          <Button onClick={() => setProfileOpen(true)}>조건 설정</Button>
        </Space>
      </section>

      <Card className="bid-notices-filter-card">
        <div className="bid-notices-filter-heading">
          <div>
            <strong>입찰공고 찾기</strong>
            <span>공고명, 공고번호, 수요기관으로 검색할 수 있어요.</span>
          </div>
          <span>{query || workTypes.length || region || publishedRange || icoreCodesOnly ? "필터 적용 중" : "전체 공고"}</span>
        </div>
        <div className="bid-notices-filter-row">
          <Input.Search
            className="bid-notices-search"
            value={queryDraft}
            placeholder="공고명, 공고번호, 수요기관 검색"
            allowClear
            onChange={(event) => setQueryDraft(event.target.value)}
            onSearch={() => { setPage(1); setQuery(queryDraft.trim()); }}
            enterButton="검색"
          />
          <Select
            mode="multiple"
            allowClear
            value={workTypes}
            placeholder="업무구분 (선택)"
            className="bid-notices-filter-select"
            options={WORK_TYPE_OPTIONS}
            onChange={(value) => { setWorkTypes(value); setPage(1); }}
          />
          <Select
            allowClear
            showSearch
            value={region}
            placeholder="지역 전체"
            className="bid-notices-filter-select"
            options={REGION_OPTIONS}
            onChange={(value) => { setRegion(value); setPage(1); }}
          />
          <RangePicker
            className="bid-notices-date-filter"
            value={publishedRange}
            onChange={(value) => { setPublishedRange(value); setPage(1); }}
            placeholder={["게시 시작일", "게시 종료일"]}
          />
          <Button onClick={() => {
            setQueryDraft("");
            setQuery("");
            setWorkTypes([]);
            setRegion(undefined);
            setPublishedRange(null);
            setIcoreCodesOnly(false);
            setPage(1);
          }}>
            필터 초기화
          </Button>
          <div className="bid-notices-icore-filter">
            <span>아이코어 기관코드</span>
            <Switch checked={icoreCodesOnly} onChange={(value) => { setIcoreCodesOnly(value); setPage(1); }} />
          </div>
        </div>
        <div className="bid-notices-condition-row">
          <Space wrap size={8} className="bid-notices-keyword-summary">
            <Typography.Text strong>내 포함 키워드</Typography.Text>
            {profileEnabled ? (
              keywords.map((keyword) => <Tag className="bid-notice-keyword-tag" key={keyword}>{keyword}</Tag>)
            ) : (
              <Tag>사용 안 함</Tag>
            )}
            {excludedKeywords.length ? (
              <>
                <Typography.Text strong>제외</Typography.Text>
                {excludedKeywords.map((keyword) => <Tag className="bid-notice-keyword-tag is-excluded" key={keyword}>{keyword}</Tag>)}
              </>
            ) : null}
          </Space>
          <Button type="link" onClick={() => setProfileOpen(true)}>조건 설정</Button>
        </div>
      </Card>

      {listError ? <Alert type="error" showIcon message="목록을 불러오지 못했습니다." description={listError} /> : null}
      <Card className="bid-notices-table-card" title={`입찰공고 목록 ${total.toLocaleString("ko-KR")}건`}>
        <Table
          rowKey="id"
          loading={loading}
          locale={{ emptyText: <Empty description="조건에 맞는 입찰공고가 없습니다." /> }}
          columns={columns}
          dataSource={rows}
          rowClassName={(row) => (selectedById.has(row.id) ? "bid-notice-row-selected" : "")}
          rowSelection={{
            columnWidth: 44,
            preserveSelectedRowKeys: true,
            selectedRowKeys,
            onSelect: toggleSelected,
            onSelectAll: togglePageSelection,
            getCheckboxProps: (row) => ({
              disabled: selectedById.size >= MAX_SELECTION_COUNT && !selectedById.has(row.id),
              title: selectedById.size >= MAX_SELECTION_COUNT && !selectedById.has(row.id)
                ? `최대 ${MAX_SELECTION_COUNT}건까지 선택할 수 있습니다.`
                : "",
            }),
          }}
          scroll={{ x: 1430 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            onChange: (nextPage, nextPageSize) => { setPage(nextPage); setPageSize(nextPageSize); },
          }}
        />
      </Card>

      {selectedById.size ? (
        <Card className="bid-notices-selection-dock">
          <div className="bid-notices-selection-row">
            <Space wrap>
              <div className="bid-notices-selection-count">{selectedById.size}</div>
              <Typography.Text strong>건 선택</Typography.Text>
              <Button onClick={clearSelection}>전체 해제</Button>
            </Space>
            <Space wrap>
              <Select
                value={destinationId}
                onChange={setDestinationId}
                placeholder="Sheet 목적지 선택"
                className="bid-notices-destination-select"
                options={destinations.map((item) => ({ value: item.id, label: `${item.label} · 개인` }))}
              />
              {destinations.length ? (
                <Button type="primary" onClick={openExportPreview} loading={previewing} disabled={!destinationId}>
                  선택한 {selectedById.size}건 검토
                </Button>
              ) : (
                <Button type="primary" onClick={openDestinationManager}>내 Sheet 연결</Button>
              )}
            </Space>
          </div>
        </Card>
      ) : null}

      <Drawer title="입찰공고 조건 설정" open={profileOpen} onClose={() => setProfileOpen(false)} width={480}>
        <div className="bid-notices-profile-form">
          <div className="bid-notices-condition-row"><div><strong>조건 사용</strong><span>포함 키워드에 맞는 공고만 검토 목록에 표시합니다.</span></div><Switch checked={profileEnabled} onChange={setProfileEnabled} loading={profileLoading} /></div>
          <div><strong>포함 키워드</strong><Select mode="tags" value={keywords} onChange={setKeywords} tokenSeparators={[","]} placeholder="예: AI, 교육, 클라우드" /></div>
          <div><strong>제외 키워드</strong><Select mode="tags" value={excludedKeywords} onChange={setExcludedKeywords} tokenSeparators={[","]} placeholder="예: 물품, 시설" /></div>
          <Button type="primary" loading={savingProfile} onClick={saveProfile}>조건 저장</Button>
        </div>
      </Drawer>

      <Drawer
        title={`14일 보관함 · ${archiveTotal.toLocaleString("ko-KR")}건`}
        open={archiveOpen}
        onClose={() => setArchiveOpen(false)}
        width={1180}
        extra={<Button onClick={() => loadArchive(archivePage)} loading={archiveLoading}>보관함 새로고침</Button>}
      >
        <Table
          rowKey="id"
          loading={archiveLoading}
          columns={archiveColumns}
          dataSource={archiveRows}
          locale={{ emptyText: <Empty description="보관된 입찰공고가 없습니다." /> }}
          scroll={{ x: 1300 }}
          pagination={{
            current: archivePage,
            pageSize,
            total: archiveTotal,
            showSizeChanger: false,
            onChange: (nextPage) => setArchivePage(nextPage),
          }}
        />
      </Drawer>

      <Modal
        title="내 입찰공고 Google Sheet 연결"
        open={connectOpen}
        width={860}
        footer={null}
        onCancel={() => setConnectOpen(false)}
      >
        <Space direction="vertical" size={20} style={{ width: "100%" }}>
          <Alert
            type={sheetServiceAccountEmail ? "info" : "warning"}
            showIcon
            message={sheetServiceAccountEmail ? "먼저 아래 서비스계정을 Google Sheet의 편집자로 공유하세요." : "서비스계정 이메일 설정이 없어 연결을 시작할 수 없습니다."}
            description={sheetServiceAccountEmail ? (
              <Space wrap>
                <Typography.Text copyable>{sheetServiceAccountEmail}</Typography.Text>
                <Button size="small" onClick={copyServiceAccountEmail}>복사</Button>
              </Space>
            ) : "백엔드의 GSHEET_SERVICE_ACCOUNT_EMAIL 설정을 관리자에게 요청하세요."}
          />

          <div className="bid-notices-destination-form">
            <div>
              <Typography.Text strong>표시 이름</Typography.Text>
              <Input value={destinationDraft.label} onChange={(event) => changeDestinationDraft("label", event.target.value)} placeholder="내 입찰공고 Sheet" />
            </div>
            <div>
              <Typography.Text strong>Google Sheet URL 또는 ID</Typography.Text>
              <Input value={destinationDraft.spreadsheet_id} onChange={(event) => changeDestinationDraft("spreadsheet_id", event.target.value)} placeholder="https://docs.google.com/spreadsheets/d/.../edit" disabled={verifyingDestination} />
            </div>
            <div>
              <Typography.Text strong>탭 이름</Typography.Text>
              <Input value={destinationDraft.tab_name} onChange={(event) => changeDestinationDraft("tab_name", event.target.value)} disabled={verifyingDestination} />
            </div>
            <div>
              <Typography.Text strong>사용 범위</Typography.Text>
              <Input value="내 개인 Sheet" disabled />
            </div>
          </div>

          {connectionError ? <Alert type="error" showIcon message="연결 테스트 실패" description={connectionError} /> : null}
          {connectionResult ? (
            <Alert
              type={connectionMeta.type}
              showIcon
              message={connectionResult.connection_ready ? "연결할 수 있습니다." : "연결을 저장할 수 없습니다."}
              description={<Space direction="vertical" size={2}><span>문서: {connectionResult.spreadsheet_title || "제목 없음"}</span><span>탭: {connectionResult.tab_exists ? `${connectionResult.tab_name} 확인` : `${connectionResult.tab_name} 없음`}</span><span>헤더: {connectionMeta.text}</span></Space>}
            />
          ) : null}

          <div className="bid-notices-destination-actions">
            <Button onClick={verifyDestination} loading={verifyingDestination} disabled={!sheetServiceAccountEmail}>연결 테스트 · 읽기 전용</Button>
            <Button type="primary" onClick={saveDestination} loading={savingDestination} disabled={!connectionResult?.connection_ready}>검증된 연결 저장</Button>
          </div>

          <div>
            <Typography.Title level={5}>등록된 내 Sheet</Typography.Title>
            {destinations.length ? (
              <div className="bid-notices-destination-list">
                {destinations.map((item) => (
                  <div className="bid-notices-destination-item" key={item.id}>
                    <div>
                      <Typography.Text strong>{item.label}</Typography.Text>
                      <Typography.Paragraph type="secondary">개인 · {item.tab_name} 탭</Typography.Paragraph>
                    </div>
                    <Space>
                      <Button type={destinationId === item.id ? "primary" : "default"} disabled={destinationId === item.id} onClick={() => { setDestinationId(item.id); setConnectOpen(false); message.success("Google Sheet 목적지를 선택했습니다."); }}>
                        {destinationId === item.id ? "사용 중" : "이 목적지 사용"}
                      </Button>
                      <Button href={sheetUrl(item.spreadsheet_id)} target="_blank" rel="noreferrer">열기</Button>
                      <Popconfirm title="이 Sheet 연결을 제거할까요?" description="기존 반영 기록과 Sheet 내용은 유지됩니다." okText="제거" cancelText="닫기" onConfirm={() => deleteDestination(item)}>
                        <Button danger>연결 제거</Button>
                      </Popconfirm>
                    </Space>
                  </div>
                ))}
              </div>
            ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="등록된 Sheet가 없습니다." />}
          </div>
        </Space>
      </Modal>

      <Modal
        title="선택한 입찰공고 Sheet 반영"
        open={Boolean(previewData)}
        width={1040}
        onCancel={closePreview}
        footer={<Space><Button onClick={closePreview} disabled={writing}>취소</Button><Button type="primary" loading={writing} onClick={confirmExport}>최종 반영</Button></Space>}
      >
        {previewError ? <Alert type="error" showIcon message={previewError} /> : null}
        <Alert type="warning" showIcon message="아직 Google Sheet에 반영하지 않았어요." description="아래 내용을 확인한 뒤 최종 반영을 눌러야 실제 쓰기가 실행됩니다." />
        <Typography.Paragraph>
          목적지: {previewData?.destination_label} · {previewData?.destination_tab_name} 탭 · 개인
        </Typography.Paragraph>
        <Table
          rowKey="key"
          size="small"
          dataSource={(previewData?.preview_rows || []).map((row, rowIndex) => Object.assign({ key: rowIndex }, ...row.map((value, index) => ({ [index]: value }))))}
          columns={(previewData?.headers || []).map((header, index) => ({ title: header, dataIndex: index, width: 150, ellipsis: true }))}
          pagination={false}
          scroll={{ x: 1500, y: 360 }}
        />
      </Modal>

      <Drawer
        title={detail?.business_name || "입찰공고 상세"}
        width={760}
        open={detailOpen}
        onClose={closeDetail}
        extra={detail?.id && !detail.from_archive ? (
          <Popconfirm title="이 공고를 검토함에서 제외할까요?" description="14일 보관함에서 다시 검토 목록으로 복구할 수 있습니다." onConfirm={() => dismissNotice(detail.id)} okText="제외" cancelText="취소">
            <Button danger>내 목록에서 제외</Button>
          </Popconfirm>
        ) : detail?.id ? (
          <Button type="primary" loading={restoringId === detail.id} onClick={() => restoreNotice(detail.id)}>검토 목록으로 복구</Button>
        ) : null}
      >
        {detailError ? <Alert type="error" showIcon message={detailError} /> : null}
        {detailLoading ? <div className="bid-notice-detail-loading">상세 정보를 불러오고 있습니다.</div> : null}
        {detail ? (
          <Descriptions bordered size="small" column={2}>
            <Descriptions.Item label="공고번호">{detail.bid_notice_no ? `${detail.bid_notice_no}-${detail.bid_notice_ord || "00"}` : "-"}</Descriptions.Item>
            <Descriptions.Item label="업무구분">{formatWorkType(detail.work_type)}</Descriptions.Item>
            <Descriptions.Item label="수요기관">{detail.demand_agency_name || "-"}</Descriptions.Item>
            <Descriptions.Item label="조달구분">{detail.procurement_type || "-"}</Descriptions.Item>
            <Descriptions.Item label="게시일시">{formatDateTime(detail.published_at)}</Descriptions.Item>
            <Descriptions.Item label="마감일시">{formatDateTime(detail.deadline_at)}</Descriptions.Item>
            <Descriptions.Item label="사업금액">{formatAmount(detail.business_amount)}</Descriptions.Item>
            <Descriptions.Item label="지역제한">{detail.region_restriction || "확인 필요"}</Descriptions.Item>
            <Descriptions.Item label="매칭 키워드">{detail.matched_keyword ? <Tag color="blue">{detail.matched_keyword}</Tag> : "-"}</Descriptions.Item>
            <Descriptions.Item label="업종제한 코드">{detail.industry_restriction_codes || (detail.industry_restriction_api_status === "API_EMPTY" ? "해당없음" : "확인하지 못함")}</Descriptions.Item>
            <Descriptions.Item label="공동수급 가능">{detail.joint_supply_allowed == null ? "" : detail.joint_supply_allowed ? "가능" : "불가"}</Descriptions.Item>
            <Descriptions.Item label="공식 공고" span={2}>
              {externalUrl(detail.notice_url) ? <Button type="link" href={externalUrl(detail.notice_url)} target="_blank" rel="noopener noreferrer">나라장터 공고 바로가기</Button> : <Text type="secondary">연결된 공식 공고 링크가 없습니다.</Text>}
            </Descriptions.Item>
          </Descriptions>
        ) : null}
        {detail ? (
          <Card className="bid-notice-attachments" size="small" title={`공고 첨부파일 ${detail.attachments.length}개`}>
            {detail.attachments.length ? (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                {detail.attachments.map((attachment) => (
                  <Button
                    key={`${attachment.label}-${attachment.url}`}
                    type="link"
                    href={externalUrl(attachment.url) || undefined}
                    target="_blank"
                    rel="noopener noreferrer"
                    disabled={!externalUrl(attachment.url)}
                  >
                    {attachment.label}
                  </Button>
                ))}
              </Space>
            ) : <Text type="secondary">나라장터 공고에 연결된 첨부파일이 없습니다.</Text>}
          </Card>
        ) : null}
      </Drawer>
    </div>
  );
}

export default BidNoticesPage;
