import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  DatePicker,
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
} from "antd";
import dayjs from "dayjs";
import { bidNoticesApi, formatApiError } from "../api/client";
import "./BidNoticesPage.css";

const { RangePicker } = DatePicker;
const { Text, Title } = Typography;

const BUSINESS_TYPE_OPTIONS = [
  { value: "SERVICE", label: "용역" },
  { value: "GOODS", label: "물품" },
  { value: "CONSTRUCTION", label: "공사" },
];

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

function externalUrl(value) {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.toString() : null;
  } catch {
    return null;
  }
}

function BidNoticesPage({ session }) {
  const requestId = useRef(0);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(30);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [workType, setWorkType] = useState();
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileLoading, setProfileLoading] = useState(false);
  const [savingProfile, setSavingProfile] = useState(false);
  const [profileEnabled, setProfileEnabled] = useState(false);
  const [keywords, setKeywords] = useState([]);
  const [excludedKeywords, setExcludedKeywords] = useState([]);
  const [collectOpen, setCollectOpen] = useState(false);
  const [collecting, setCollecting] = useState(false);
  const [collectionRange, setCollectionRange] = useState([dayjs().subtract(14, "day"), dayjs()]);
  const [businessTypes, setBusinessTypes] = useState(["SERVICE"]);
  const [selectedRowKeys, setSelectedRowKeys] = useState([]);
  const [exportOpen, setExportOpen] = useState(false);
  const [destinations, setDestinations] = useState([]);
  const [destinationId, setDestinationId] = useState();
  const [destinationsLoading, setDestinationsLoading] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [writing, setWriting] = useState(false);
  const [exportPreview, setExportPreview] = useState(null);
  const [connectOpen, setConnectOpen] = useState(false);
  const [connecting, setConnecting] = useState(false);
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

  const canCollect = session?.role === "admin";
  const profileSummary = useMemo(() => {
    if (!profileEnabled) return "조건 설정이 꺼져 있습니다.";
    if (!keywords.length) return "포함 키워드를 입력하세요.";
    return `포함 ${keywords.length}개 · 제외 ${excludedKeywords.length}개`;
  }, [excludedKeywords.length, keywords.length, profileEnabled]);

  const loadList = () => {
    const nextRequestId = requestId.current + 1;
    requestId.current = nextRequestId;
    setLoading(true);
    setListError("");
    bidNoticesApi
      .list({ page, page_size: pageSize, q: query || undefined, work_type: workType })
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
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 조건을 불러오지 못했습니다."));
    } finally {
      setProfileLoading(false);
    }
  };

  useEffect(() => {
    loadList();
  }, [page, pageSize, query, workType]);

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
      message.success("입찰공고 조건을 저장했습니다.");
      loadList();
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 조건 저장에 실패했습니다."));
    } finally {
      setSavingProfile(false);
    }
  };

  const runCollection = async () => {
    if (!collectionRange?.[0] || !collectionRange?.[1]) {
      message.warning("수집 기간을 선택하세요.");
      return;
    }
    if (!profileEnabled || !keywords.length) {
      message.warning("수집하려면 조건 설정에서 포함 키워드를 저장하고 조건 사용을 켜세요.");
      setCollectOpen(false);
      setProfileOpen(true);
      return;
    }
    setCollecting(true);
    try {
      const response = await bidNoticesApi.collect({
        start_date: collectionRange[0].format("YYYY-MM-DD"),
        end_date: collectionRange[1].format("YYYY-MM-DD"),
        business_types: businessTypes,
      });
      setCollectOpen(false);
      message.success(`입찰공고 ${response.data.fetched_count}건을 수집했습니다.`);
      setPage(1);
      loadList();
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 수집에 실패했습니다."));
    } finally {
      setCollecting(false);
    }
  };

  const refreshReview = async () => {
    setSelectedRowKeys([]);
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
      setSelectedRowKeys((current) => current.filter((id) => id !== noticeId));
      setRows((current) => current.filter((row) => row.id !== noticeId));
      setTotal((current) => Math.max(0, current - 1));
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
      message.success("검토 목록으로 복구했습니다.");
    } catch (error) {
      message.error(formatApiError(error, "입찰공고 복구에 실패했습니다."));
    } finally {
      setRestoringId(null);
    }
  };

  const loadDestinations = async () => {
    setDestinationsLoading(true);
    try {
      const response = await bidNoticesApi.listSheetDestinations();
      const nextDestinations = response.data || [];
      setDestinations(nextDestinations);
      setDestinationId((current) => current || nextDestinations[0]?.id);
    } catch (error) {
      message.error(formatApiError(error, "내 Google Sheet 연결을 불러오지 못했습니다."));
    } finally {
      setDestinationsLoading(false);
    }
  };

  const openExport = () => {
    if (!selectedRowKeys.length) {
      message.warning("Sheet에 반영할 입찰공고를 선택하세요.");
      return;
    }
    setExportPreview(null);
    setExportOpen(true);
    loadDestinations();
  };

  const previewExport = async () => {
    if (!destinationId) {
      message.warning("내 Google Sheet 목적지를 선택하세요.");
      return;
    }
    setPreviewing(true);
    try {
      const response = await bidNoticesApi.exportSheet({
        destination_id: destinationId,
        notice_ids: selectedRowKeys,
        dry_run: true,
      });
      setExportPreview(response.data);
    } catch (error) {
      message.error(formatApiError(error, "Sheet 미리보기를 만들지 못했습니다."));
    } finally {
      setPreviewing(false);
    }
  };

  const writeExport = async () => {
    if (!exportPreview?.preview_token) {
      message.warning("먼저 미리보기를 확인하세요.");
      return;
    }
    setWriting(true);
    try {
      const response = await bidNoticesApi.exportSheet({
        destination_id: destinationId,
        notice_ids: selectedRowKeys,
        dry_run: false,
        expected_preview_token: exportPreview.preview_token,
      });
      setExportOpen(false);
      setExportPreview(null);
      setSelectedRowKeys([]);
      message.success(`Sheet 반영 완료: 신규 ${response.data.inserted_count}건 · 갱신 ${response.data.updated_count}건`);
    } catch (error) {
      message.error(formatApiError(error, "Google Sheet 반영에 실패했습니다."));
    } finally {
      setWriting(false);
    }
  };

  const saveDestination = async () => {
    if (!destinationDraft.label || !destinationDraft.spreadsheet_id || !destinationDraft.tab_name) {
      message.warning("이름, Sheet URL, 탭 이름을 입력하세요.");
      return;
    }
    setConnecting(true);
    try {
      const verification = await bidNoticesApi.verifySheetDestination(destinationDraft);
      if (!verification.data.connection_ready) {
        message.error("지정한 탭이 없거나 헤더가 다른 형식입니다. 빈 탭 또는 입찰공고 헤더 탭을 선택하세요.");
        return;
      }
      const saved = await bidNoticesApi.saveSheetDestination(destinationDraft);
      setConnectOpen(false);
      setDestinationId(saved.data.id);
      setExportPreview(null);
      message.success("개인 Google Sheet 연결을 저장했습니다.");
      await loadDestinations();
    } catch (error) {
      message.error(formatApiError(error, "Google Sheet 연결 저장에 실패했습니다."));
    } finally {
      setConnecting(false);
    }
  };

  const deleteDestination = async (target) => {
    try {
      await bidNoticesApi.deleteSheetDestination(target.id);
      setDestinationId((current) => (current === target.id ? undefined : current));
      await loadDestinations();
      message.success("개인 Google Sheet 연결을 제거했습니다.");
    } catch (error) {
      message.error(formatApiError(error, "Google Sheet 연결 제거에 실패했습니다."));
    }
  };

  const openDestinationManager = async () => {
    await loadDestinations();
    setConnectOpen(true);
  };

  const columns = [
    {
      title: "공고명",
      dataIndex: "business_name",
      key: "business_name",
      width: 340,
      render: (value, row) => {
        const url = externalUrl(row.notice_url);
        return (
          <div className="bid-notice-title-cell">
            {url ? <a href={url} target="_blank" rel="noreferrer">{value || "공고명 미확인"}</a> : <strong>{value || "공고명 미확인"}</strong>}
            <span>{row.demand_agency_name || "수요기관 미확인"}</span>
          </div>
        );
      },
    },
    {
      title: "공고번호",
      key: "notice_no",
      width: 175,
      render: (_, row) => `${row.bid_notice_no || "-"}-${row.bid_notice_ord || "00"}`,
    },
    { title: "업무", dataIndex: "work_type", key: "work_type", width: 86, render: (value) => value || "-" },
    { title: "게시", dataIndex: "published_at", key: "published_at", width: 145, render: formatDateTime },
    { title: "마감", dataIndex: "deadline_at", key: "deadline_at", width: 145, render: formatDateTime },
    { title: "사업금액", dataIndex: "business_amount", key: "business_amount", width: 145, render: formatAmount },
    { title: "기초금액", dataIndex: "official_base_amount", key: "official_base_amount", width: 145, render: formatAmount },
    {
      title: "조건",
      key: "condition",
      width: 145,
      render: (_, row) => (
        <Space size={4} wrap>
          {row.matched_keyword ? <Tag color="blue">{row.matched_keyword}</Tag> : null}
          {row.region_restriction ? <Tag>{row.region_restriction}</Tag> : <Tag>지역 확인 필요</Tag>}
        </Space>
      ),
    },
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
    ...columns.slice(0, -1),
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
          <Text>공통 원본을 수집한 뒤, 내 포함·제외 키워드에 맞는 공고만 확인합니다.</Text>
        </div>
        <Space wrap>
          <Button onClick={() => setProfileOpen(true)}>조건 설정</Button>
          <Button onClick={refreshReview} loading={loading}>새로고침</Button>
          <Button onClick={() => setArchiveOpen(true)}>14일 보관함</Button>
          <Button onClick={openDestinationManager}>Sheet 연결 관리</Button>
          <Button onClick={openExport}>Sheet 반영 ({selectedRowKeys.length})</Button>
          {canCollect ? <Button type="primary" onClick={() => setCollectOpen(true)}>입찰공고 수집</Button> : null}
        </Space>
      </section>

      <Card className="bid-notices-filter-card">
        <div className="bid-notices-filter-heading">
          <div><strong>내 수집 조건</strong><span>{profileSummary}</span></div>
          <Button type="link" onClick={() => setProfileOpen(true)}>조건 설정</Button>
        </div>
        <Space wrap>
          <Input.Search
            className="bid-notices-search"
            value={queryDraft}
            placeholder="공고명 검색"
            allowClear
            onChange={(event) => setQueryDraft(event.target.value)}
            onSearch={() => { setPage(1); setQuery(queryDraft.trim()); }}
          />
          <Select
            allowClear
            value={workType}
            placeholder="업무구분 전체"
            style={{ width: 140 }}
            options={BUSINESS_TYPE_OPTIONS.map((item) => ({ value: item.label, label: item.label }))}
            onChange={(value) => { setWorkType(value); setPage(1); }}
          />
          <Button onClick={() => { setQueryDraft(""); setQuery(""); setWorkType(undefined); setPage(1); }}>필터 초기화</Button>
        </Space>
      </Card>

      {listError ? <Alert type="error" showIcon message="목록을 불러오지 못했습니다." description={listError} /> : null}
      <Card className="bid-notices-table-card" title={`검토 목록 ${total.toLocaleString("ko-KR")}건`}>
        <Table
          rowKey="id"
          loading={loading}
          locale={{ emptyText: <Empty description="조건에 맞는 입찰공고가 없습니다." /> }}
          columns={columns}
          dataSource={rows}
          rowSelection={{
            selectedRowKeys,
            onChange: (nextKeys) => { setSelectedRowKeys(nextKeys); setExportPreview(null); },
            preserveSelectedRowKeys: true,
          }}
          scroll={{ x: 1320 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            onChange: (nextPage, nextPageSize) => { setPage(nextPage); setPageSize(nextPageSize); },
          }}
        />
      </Card>

      <Drawer title="입찰공고 조건 설정" open={profileOpen} onClose={() => setProfileOpen(false)} width={480}>
        <div className="bid-notices-profile-form">
          <div className="bid-notices-condition-row"><div><strong>조건 사용</strong><span>포함 키워드에 맞는 공고만 검토 목록에 표시합니다.</span></div><Switch checked={profileEnabled} onChange={setProfileEnabled} loading={profileLoading} /></div>
          <div><strong>포함 키워드</strong><Select mode="tags" value={keywords} onChange={setKeywords} tokenSeparators={[","]} placeholder="예: AI, 교육, 클라우드" /></div>
          <div><strong>제외 키워드</strong><Select mode="tags" value={excludedKeywords} onChange={setExcludedKeywords} tokenSeparators={[","]} placeholder="예: 물품, 시설" /></div>
          <Button type="primary" loading={savingProfile} onClick={saveProfile}>조건 저장</Button>
        </div>
      </Drawer>

      <Modal title="입찰공고 수집" open={collectOpen} onCancel={() => setCollectOpen(false)} onOk={runCollection} confirmLoading={collecting} okText="수집 실행">
        <div className="bid-notices-collect-form">
          <div><strong>게시일 기준 기간</strong><RangePicker value={collectionRange} onChange={setCollectionRange} allowClear={false} /></div>
          <div><strong>업무구분</strong><Select mode="multiple" value={businessTypes} onChange={setBusinessTypes} options={BUSINESS_TYPE_OPTIONS} /></div>
          <Alert type="info" showIcon message="첨부파일·문서 분석은 이번 수집에 포함하지 않습니다." />
        </div>
      </Modal>

      <Modal
        title="선택한 입찰공고를 Sheet에 반영"
        open={exportOpen}
        onCancel={() => setExportOpen(false)}
        footer={[
          <Button key="close" onClick={() => setExportOpen(false)}>닫기</Button>,
          <Button key="preview" loading={previewing} onClick={previewExport}>미리보기</Button>,
          <Button key="write" type="primary" disabled={!exportPreview} loading={writing} onClick={writeExport}>최종 반영</Button>,
        ]}
      >
        <div className="bid-notices-export-form">
          <Text>선택 {selectedRowKeys.length}건만 내 개인 Google Sheet에 반영합니다.</Text>
          <Select
            loading={destinationsLoading}
            value={destinationId}
            placeholder="개인 Sheet 선택"
            onChange={(value) => { setDestinationId(value); setExportPreview(null); }}
            options={destinations.map((item) => ({ value: item.id, label: `${item.label} · ${item.tab_name}` }))}
          />
          <Button type="link" onClick={openDestinationManager}>새 개인 Sheet 연결</Button>
          {exportPreview ? <Alert type="info" showIcon message={`${exportPreview.row_count}건 미리보기 완료`} description={`대상: ${exportPreview.destination_label} · ${exportPreview.destination_tab_name}`} /> : null}
        </div>
      </Modal>

      <Modal title="14일 보관함" open={archiveOpen} onCancel={() => setArchiveOpen(false)} footer={null} width={1180}>
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
      </Modal>

      <Modal title="개인 Google Sheet 연결 관리" open={connectOpen} onCancel={() => setConnectOpen(false)} onOk={saveDestination} confirmLoading={connecting} okText="연결 확인 후 저장">
        <div className="bid-notices-export-form">
          {destinations.length ? <div className="bid-notices-destination-list">{destinations.map((item) => <Space key={item.id} className="bid-notices-destination-item"><Button type={destinationId === item.id ? "primary" : "default"} onClick={() => setDestinationId(item.id)}>{item.label} · {item.tab_name}</Button><Popconfirm title="이 개인 Sheet 연결을 제거할까요?" onConfirm={() => deleteDestination(item)} okText="제거" cancelText="취소"><Button type="link" danger>제거</Button></Popconfirm></Space>)}</div> : <Text type="secondary">저장된 개인 Sheet 연결이 없습니다.</Text>}
          <Input value={destinationDraft.label} placeholder="연결 이름" onChange={(event) => setDestinationDraft((draft) => ({ ...draft, label: event.target.value }))} />
          <Input value={destinationDraft.spreadsheet_id} placeholder="Google Sheet URL 또는 ID" onChange={(event) => setDestinationDraft((draft) => ({ ...draft, spreadsheet_id: event.target.value }))} />
          <Input value={destinationDraft.tab_name} placeholder="탭 이름" onChange={(event) => setDestinationDraft((draft) => ({ ...draft, tab_name: event.target.value }))} />
          <Alert type="info" showIcon message="서비스계정에 편집 권한을 공유한 빈 탭 또는 입찰공고 형식 탭만 저장할 수 있습니다." />
        </div>
      </Modal>
    </div>
  );
}

export default BidNoticesPage;
