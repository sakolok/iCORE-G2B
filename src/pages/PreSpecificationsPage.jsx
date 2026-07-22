import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import dayjs from "dayjs";
import { formatApiError, preSpecificationsApi } from "../api/client";
import "./PreSpecificationsPage.css";

const { RangePicker } = DatePicker;

const DEADLINE_META = {
  OPEN: { color: "green", label: "의견 접수 중" },
  TODAY: { color: "gold", label: "오늘 마감" },
  CLOSED: { color: "default", label: "마감" },
  UNKNOWN: { color: "default", label: "마감일 미확인" },
};

const DEFAULT_COLLECTION_RANGE = [dayjs().subtract(13, "day"), dayjs()];

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

function PreSpecificationsPage({ session }) {
  const listRequestId = useRef(0);
  const detailRequestId = useRef(0);
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
  const [collectionRange, setCollectionRange] = useState(DEFAULT_COLLECTION_RANGE);
  const [collecting, setCollecting] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

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
        setRows(response.data.items || []);
        setTotal(response.data.total || 0);
        setLastLoadedAt(dayjs());
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

  const openDetail = async (row) => {
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    setDetailOpen(true);
    setDetail(null);
    setDetailError("");
    setDetailLoading(true);
    try {
      const response = await preSpecificationsApi.detail(row.bf_spec_rgst_no);
      if (detailRequestId.current === requestId) setDetail(response.data);
    } catch (error) {
      if (detailRequestId.current === requestId) {
        setDetailError(formatApiError(error, "사전규격 상세를 불러오지 못했습니다."));
      }
    } finally {
      if (detailRequestId.current === requestId) setDetailLoading(false);
    }
  };

  const collectRows = async () => {
    if (!collectionRange?.[0] || !collectionRange?.[1]) {
      message.warning("수집 기간을 선택해주세요.");
      return;
    }
    setCollecting(true);
    try {
      const response = await preSpecificationsApi.collect({
        start_date: collectionRange[0].format("YYYY-MM-DD"),
        end_date: collectionRange[1].format("YYYY-MM-DD"),
      });
      const result = response.data;
      message.success(
        `사전규격 ${result.fetched_count}건을 확인해 ${result.inserted_count}건 추가, ${result.updated_count}건 갱신했습니다.`
      );
      setPage(1);
      setReloadKey((current) => current + 1);
    } catch (error) {
      message.error(formatApiError(error, "사전규격을 수집하지 못했습니다."));
    } finally {
      setCollecting(false);
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
      width: 360,
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
      width: 90,
      align: "center",
      render: (attachments = []) => (attachments.length ? `${attachments.length}개` : "-"),
    },
    {
      title: "작업",
      key: "actions",
      width: 100,
      fixed: "right",
      render: (_, row) => (
        <Button size="small" onClick={() => openDetail(row)}>
          상세보기
        </Button>
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

  return (
    <section className="pre-specifications-page" aria-labelledby="pre-specifications-title">
      <header className="pre-specifications-hero">
        <div>
          <span className="pre-specifications-eyebrow">나라장터 사전규격</span>
          <Typography.Title id="pre-specifications-title" level={2}>
            사전규격을 검토해요
          </Typography.Title>
          <Typography.Paragraph>
            공고 전 공개된 규격을 한곳에서 확인하고 검토할 수 있습니다.
          </Typography.Paragraph>
        </div>

        <div className="pre-specifications-hero-actions">
          {lastLoadedAt ? (
            <Typography.Text type="secondary" className="pre-specifications-loaded-at">
              DB 조회 {lastLoadedAt.format("MM.DD HH:mm:ss")}
            </Typography.Text>
          ) : null}
          <Button onClick={() => setReloadKey((current) => current + 1)} loading={loading}>
            목록 새로고침
          </Button>
          {session?.role === "admin" ? (
            <Space.Compact className="pre-specifications-collect-controls">
              <RangePicker
                value={collectionRange}
                onChange={setCollectionRange}
                allowClear={false}
                disabledDate={(current) => current && current > dayjs().endOf("day")}
              />
              <Button type="primary" onClick={collectRows} loading={collecting}>
                기간 수집
              </Button>
            </Space.Compact>
          ) : null}
        </div>
      </header>

      {listError ? <Alert type="error" showIcon message={listError} /> : null}

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
      </Card>

      <Card className="pre-specifications-table-card" title="검토할 사전규격">
        <Table
          rowKey="bf_spec_rgst_no"
          columns={columns}
          dataSource={rows}
          loading={loading}
          scroll={{ x: 1190 }}
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

      <Drawer
        title="사전규격 상세"
        width={640}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
      >
        {detailError ? <Alert type="error" showIcon message={detailError} /> : null}
        {detailLoading ? (
          <div className="pre-specifications-detail-loading">상세 내용을 불러오고 있습니다.</div>
        ) : null}
        {detail ? (
          <div className="pre-specifications-detail">
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
