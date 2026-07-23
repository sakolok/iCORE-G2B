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
      .list({ page, page_size: pageSize, q: query || undefined })
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
  }, [page, pageSize, query]);

  useEffect(() => {
    loadProfile();
  }, []);

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
          <Button onClick={() => { setQueryDraft(""); setQuery(""); setPage(1); }}>초기화</Button>
          <Button onClick={loadList} loading={loading}>새로고침</Button>
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
    </div>
  );
}

export default BidNoticesPage;
