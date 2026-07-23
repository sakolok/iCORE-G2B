import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  DatePicker,
  Divider,
  Empty,
  Input,
  InputNumber,
  Modal,
  Popover,
  Popconfirm,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  TimePicker,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  ApartmentOutlined,
  BellOutlined,
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  MailOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { fetchBidNoticePreview, saveSelectedBidNotices } from "./bidNoticePreviewApi";
import "./G2BCollectionSettingsPreview.css";

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;

const WORK_TYPE_OPTIONS = ["공사", "기술용역", "물품", "민간물품", "일반용역"];
const PROCUREMENT_TYPE_OPTIONS = ["내자", "외자"];
const REGION_OPTIONS = ["서울특별시", "경기도", "부산광역시", "대전광역시", "전국"];
const SHEET_OPTIONS = [
  "내 연수교육 공고 Sheet",
  "내 AI 교육 공고 Sheet",
  "내 대학교 교육사업 Sheet",
];
const DEFAULT_POSTED_DATE_START = dayjs().subtract(14, "day").format("YYYY-MM-DD");
const DEFAULT_POSTED_DATE_END = dayjs().format("YYYY-MM-DD");

const INITIAL_ORGANIZATION_GROUPS = [
  {
    id: "education-office",
    name: "교육청·교육지원청",
    parentAgencies: ["서울특별시교육청", "경기도교육청", "부산광역시교육청"],
    childAgencies: ["서울특별시강남서초교육지원청", "서울특별시교육청 AI교육지원센터"],
    aliases: ["서울시교육청", "교육청", "교육지원청"],
    codes: ["EDU-SEOUL", "EDU-GYEONGGI", "EDU-BUSAN"],
  },
  {
    id: "university",
    name: "대학교·산학협력단",
    parentAgencies: ["한빛대학교", "새봄대학교"],
    childAgencies: ["한빛대학교 산학협력단", "새봄대학교 산학협력단"],
    aliases: ["대학", "산학협력단"],
    codes: ["UNI-HANBIT", "UNI-SAEBOM"],
  },
  {
    id: "public-agency",
    name: "공공기관·산하기관",
    parentAgencies: ["한국디지털진흥원", "한국교육진흥원"],
    childAgencies: ["한빛테크노파크", "디지털교육센터"],
    aliases: ["공공기관", "테크노파크", "TP"],
    codes: ["PUB-DIGI", "PUB-EDU", "PUB-TP"],
  },
];

const INITIAL_COLLECTION_SETTINGS = [
  {
    id: "training",
    name: "연수교육 공고",
    memo: "교육청 및 교육지원청의 연수교육 용역 공고를 확인합니다.",
    workTypes: [],
    requiredKeywords: ["ai"],
    excludedKeywords: ["시설", "공사", "유지보수", "물품"],
    baseAmountMin: null,
    baseAmountMax: null,
    participationRegions: [],
    postedDateStart: DEFAULT_POSTED_DATE_START,
    postedDateEnd: DEFAULT_POSTED_DATE_END,
    recipients: ["training@example.com"],
    instantAlert: true,
    digestTime: "17:30",
    sheet: "내 연수교육 공고 Sheet",
  },
  {
    id: "ai-education",
    name: "AI 교육 공고",
    memo: "AI 교육 프로그램·과정 운영 사업을 분리해 확인합니다.",
    workTypes: [],
    requiredKeywords: ["AI", "교육"],
    excludedKeywords: ["시설", "공사", "유지보수"],
    baseAmountMin: null,
    baseAmountMax: null,
    participationRegions: [],
    postedDateStart: "",
    postedDateEnd: "",
    recipients: ["ai-education@example.com"],
    instantAlert: true,
    digestTime: "17:30",
    sheet: "내 AI 교육 공고 Sheet",
  },
  {
    id: "university-education",
    name: "대학교 교육사업",
    memo: "대학교·공공기관의 교육사업과 소규모 물품 사업을 확인합니다.",
    workTypes: [],
    requiredKeywords: [],
    excludedKeywords: [],
    baseAmountMin: null,
    baseAmountMax: null,
    participationRegions: [],
    postedDateStart: "",
    postedDateEnd: "",
    recipients: ["university@example.com"],
    instantAlert: true,
    digestTime: "17:30",
    sheet: "내 대학교 교육사업 Sheet",
  },
];

const SAMPLE_NOTICES = [
  {
    id: "notice-training-priority",
    bidNoticeNo: "2026-000123",
    bidNoticeOrd: "00",
    bidNtceNm: "2026학년도 교원 직무연수 교육 운영 용역",
    demandAgencyName: "서울특별시교육청",
    demandAgencyCode: "EDU-SEOUL",
    workType: "일반용역",
    procurementType: "내자",
    baseAmount: 128_000_000,
    participationRegions: ["서울특별시"],
    proposalDeadline: "2026-08-22T15:00:00+09:00",
    detailStatus: "DETAIL_COMPLETED",
  },
  {
    id: "notice-training-review",
    bidNoticeNo: "2026-000124",
    bidNoticeOrd: "000",
    bidNtceNm: "2026 교직원 연수 교육 운영 용역",
    demandAgencyName: "서울특별시강남서초교육지원청",
    demandAgencyCode: "EDU-SEOUL",
    workType: "일반용역",
    procurementType: "내자",
    baseAmount: null,
    participationRegions: null,
    proposalDeadline: null,
    detailStatus: "DETAIL_REQUIRED",
  },
  {
    id: "notice-training-exclude",
    bidNoticeNo: "2026-000125",
    bidNoticeOrd: "00",
    bidNtceNm: "교육연수원 시설 유지보수 공사",
    demandAgencyName: "부산광역시교육청",
    demandAgencyCode: "EDU-BUSAN",
    workType: "공사",
    procurementType: "내자",
    baseAmount: 72_000_000,
    participationRegions: ["부산광역시"],
    proposalDeadline: null,
    detailStatus: "SOURCE_MISSING",
  },
  {
    id: "notice-ai-priority",
    bidNoticeNo: "2026-000301",
    bidNoticeOrd: "00",
    bidNtceNm: "생성형 AI 교육과정 개발 및 운영 용역",
    demandAgencyName: "한국디지털진흥원",
    demandAgencyCode: "PUB-DIGI",
    workType: "일반용역",
    procurementType: "내자",
    baseAmount: 220_000_000,
    participationRegions: ["전국"],
    proposalDeadline: "2026-09-10T14:00:00+09:00",
    detailStatus: "LIST_ONLY",
  },
  {
    id: "notice-ai-agency-keyword",
    bidNoticeNo: "2026-000302",
    bidNoticeOrd: "00",
    bidNtceNm: "2026 미래교육 프로그램 운영 용역",
    demandAgencyName: "서울특별시교육청 AI교육지원센터",
    demandAgencyCode: "EDU-SEOUL",
    workType: "일반용역",
    procurementType: "내자",
    baseAmount: 96_000_000,
    participationRegions: ["서울특별시"],
    proposalDeadline: "2026-09-18T14:00:00+09:00",
    detailStatus: "DETAIL_COMPLETED",
  },
  {
    id: "notice-university-service",
    bidNoticeNo: "2026-000201",
    bidNoticeOrd: "00",
    bidNtceNm: "대학 교육 프로그램 운영 용역",
    demandAgencyName: "한빛대학교 산학협력단",
    demandAgencyCode: "UNI-HANBIT",
    workType: "일반용역",
    procurementType: "내자",
    baseAmount: 145_000_000,
    participationRegions: ["전국"],
    proposalDeadline: "2026-08-30T16:00:00+09:00",
    detailStatus: "DETAIL_COMPLETED",
  },
  {
    id: "notice-university-goods",
    bidNoticeNo: "2026-000202",
    bidNoticeOrd: "00",
    bidNtceNm: "교육용 크롬북 구매",
    demandAgencyName: "한빛테크노파크",
    demandAgencyCode: "PUB-TP",
    workType: "물품",
    procurementType: "내자",
    baseAmount: null,
    participationRegions: null,
    proposalDeadline: null,
    detailStatus: "DETAIL_REQUIRED",
  },
];

function updateSetting(settings, id, patch) {
  return settings.map((setting) => (setting.id === id ? { ...setting, ...patch } : setting));
}

function formatAmount(amount) {
  return amount === null || amount === undefined ? "—" : `${amount.toLocaleString("ko-KR")}원`;
}

function formatDeadline(deadline) {
  return deadline ? dayjs(deadline).format("YYYY.MM.DD HH:mm") : "—";
}

function SettingPicker({ settings, activeSettingId, onSelect, onAdd, onDuplicate, onDelete, onOpenDeliverySettings }) {
  const activeSetting = settings.find((setting) => setting.id === activeSettingId);
  return (
    <aside className="collection-setting-picker">
      <div className="collection-picker-heading">
        <div>
          <Text className="collection-section-label">내 설정</Text>
          <Title level={4}>수집 설정 목록</Title>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={onAdd}>추가</Button>
      </div>
      <Text type="secondary" className="collection-picker-description">하나의 설정만 사용해도 됩니다. 사업을 분리할 때만 추가하세요.</Text>
      <div className="collection-setting-list">
        {settings.map((setting) => (
          <button
            type="button"
            key={setting.id}
            className={`collection-setting-option ${setting.id === activeSettingId ? "is-active" : ""}`}
            onClick={() => onSelect(setting.id)}
          >
            <span className="collection-setting-option-name">{setting.name}</span>
            <span className="collection-setting-option-meta">{setting.sheet || "Sheets 저장 위치 미설정"}</span>
          </button>
        ))}
      </div>
      <Divider />
      <Space wrap>
        <Button icon={<BellOutlined />} disabled={!activeSetting} onClick={onOpenDeliverySettings}>알림·저장 설정</Button>
        <Button icon={<CopyOutlined />} disabled={!activeSetting} onClick={onDuplicate}>복제</Button>
        <Popconfirm
          title="이 수집 설정을 삭제할까요?"
          description="이 브라우저의 현재 시안에서만 삭제됩니다."
          okText="삭제"
          cancelText="취소"
          onConfirm={onDelete}
          disabled={settings.length <= 1}
        >
          <Button danger icon={<DeleteOutlined />} disabled={settings.length <= 1}>삭제</Button>
        </Popconfirm>
      </Space>
    </aside>
  );
}

function DeliverySettingsModal({ open, setting, onClose, onChange, onSave }) {
  if (!setting) return null;

  return (
    <Modal
      open={open}
      onCancel={onClose}
      onOk={() => {
        onSave();
        onClose();
      }}
      okText="저장"
      cancelText="취소"
      title={`알림·저장 설정 — ${setting.name}`}
      width={640}
    >
      <div className="collection-field"><Text strong>수신 이메일</Text><Select mode="tags" tokenSeparators={[","]} value={setting.recipients} onChange={(value) => onChange({ recipients: value })} placeholder="name@example.com" /></div>
      <div className="collection-field"><Text strong>Google Sheets 저장 위치</Text><Select value={setting.sheet} onChange={(value) => onChange({ sheet: value })} options={SHEET_OPTIONS.map((value) => ({ value, label: value }))} /></div>
      <div className="collection-delivery-grid">
        <div className="collection-delivery-item"><Space><BellOutlined /><Text strong>우선 검토 공고 즉시 알림</Text></Space><Switch checked={setting.instantAlert} onChange={(value) => onChange({ instantAlert: value })} /></div>
        <div className="collection-delivery-item"><Space><MailOutlined /><Text strong>확인 필요 공고 요약 메일 시간</Text></Space><TimePicker format="HH:mm" value={setting.digestTime ? dayjs(setting.digestTime, "HH:mm") : null} onChange={(value) => onChange({ digestTime: value ? value.format("HH:mm") : "" })} /></div>
      </div>
    </Modal>
  );
}

function DeliverySettingsPanel({ setting, onChange }) {
  return (
    <Card className="collection-delivery-card">
      <Text className="collection-section-label">알림·저장 설정</Text>
      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}><div className="collection-field"><Text strong>수신 이메일</Text><Select mode="tags" tokenSeparators={[","]} value={setting.recipients} onChange={(value) => onChange({ recipients: value })} placeholder="name@example.com" /></div></Col>
        <Col xs={24} md={12}><div className="collection-field"><Text strong>Google Sheets 저장 위치</Text><Select value={setting.sheet} onChange={(value) => onChange({ sheet: value })} options={SHEET_OPTIONS.map((value) => ({ value, label: value }))} /></div></Col>
      </Row>
      <div className="collection-delivery-grid">
        <div className="collection-delivery-item"><Space><BellOutlined /><Text strong>우선 검토 공고 즉시 알림</Text></Space><Switch checked={setting.instantAlert} onChange={(value) => onChange({ instantAlert: value })} /></div>
        <div className="collection-delivery-item"><Space><MailOutlined /><Text strong>확인 필요 공고 요약 메일 시간</Text></Space><TimePicker format="HH:mm" value={setting.digestTime ? dayjs(setting.digestTime, "HH:mm") : null} onChange={(value) => onChange({ digestTime: value ? value.format("HH:mm") : "" })} /></div>
      </div>
    </Card>
  );
}

function CollectionTabsHeader({ setting, settings, activeSettingId, onSelect, onAdd, onDuplicate, onDelete, onOpenDeliverySettings }) {
  const tabItems = settings.map((item) => ({ key: item.id, label: item.name || "이름 없는 탭" }));
  const recipientText = setting.recipients.length ? setting.recipients.join(", ") : "수신자 미설정";

  return (
    <section className="collection-tab-panel">
      <Tabs
        activeKey={activeSettingId}
        type="card"
        items={tabItems}
        onChange={onSelect}
        tabBarExtraContent={<Button type="text" icon={<PlusOutlined />} onClick={onAdd}>새 탭</Button>}
      />
      <div className="collection-delivery-summary">
        <div>
          <Text strong>알림·저장 설정</Text>
          <Space wrap size={[6, 6]} className="collection-delivery-tags">
            <Tag icon={<MailOutlined />}>{recipientText}</Tag>
            <Tag icon={<BellOutlined />} color={setting.instantAlert ? "blue" : "default"}>{setting.instantAlert ? "우선 검토 즉시 알림" : "즉시 알림 끔"}</Tag>
            <Tag>요약 {setting.digestTime || "시간 미설정"}</Tag>
            <Tag icon={<DatabaseOutlined />} color="purple">{setting.sheet || "Sheets 미설정"}</Tag>
          </Space>
        </div>
        <Space wrap>
          <Button icon={<SettingOutlined />} onClick={onOpenDeliverySettings}>수정</Button>
          <Button icon={<CopyOutlined />} onClick={onDuplicate}>복제</Button>
          <Popconfirm title="이 수집 탭을 삭제할까요?" description="이 브라우저의 현재 시안에서만 삭제됩니다." okText="삭제" cancelText="취소" onConfirm={() => onDelete(setting.id)} disabled={settings.length <= 1}>
            <Button danger icon={<DeleteOutlined />} disabled={settings.length <= 1}>삭제</Button>
          </Popconfirm>
        </Space>
      </div>
    </section>
  );
}

function OrganizationGroupModal({ open, groups, onClose, onSaveGroups }) {
  const [editingId, setEditingId] = useState(null);
  const [draft, setDraft] = useState(null);
  const activeGroup = groups.find((group) => group.id === editingId);

  const startCreate = () => {
    setEditingId("new");
    setDraft({ id: `group-${Date.now()}`, name: "", parentAgencies: [], childAgencies: [], aliases: [], codes: [] });
  };

  const startEdit = (group) => {
    setEditingId(group.id);
    setDraft({ ...group, parentAgencies: [...group.parentAgencies], childAgencies: [...group.childAgencies], aliases: [...group.aliases], codes: [...group.codes] });
  };

  const saveDraft = () => {
    if (!draft?.name.trim()) {
      message.warning("기관 그룹 이름을 입력해주세요.");
      return;
    }
    const nextGroups = groups.some((group) => group.id === draft.id)
      ? groups.map((group) => (group.id === draft.id ? { ...draft, name: draft.name.trim() } : group))
      : [...groups, { ...draft, name: draft.name.trim() }];
    onSaveGroups(nextGroups);
    setEditingId(null);
    setDraft(null);
    message.success("기관 범위를 이 브라우저에 반영했습니다.");
  };

  const deleteGroup = (groupId) => {
    onSaveGroups(groups.filter((group) => group.id !== groupId));
    if (editingId === groupId) {
      setEditingId(null);
      setDraft(null);
    }
  };

  const columns = [
    { title: "기관 그룹", dataIndex: "name", width: 180, render: (value) => <Text strong>{value}</Text> },
    { title: "대표기관", dataIndex: "parentAgencies", render: (value) => value.join(", ") || "—" },
    { title: "산하기관", dataIndex: "childAgencies", render: (value) => value.join(", ") || "—" },
    { title: "별칭", dataIndex: "aliases", render: (value) => value.join(", ") || "—" },
    { title: "기관코드", dataIndex: "codes", render: (value) => value.join(", ") || "—" },
    {
      title: "관리",
      key: "manage",
      width: 120,
      render: (_, group) => (
        <Space size={2}>
          <Button type="link" icon={<EditOutlined />} onClick={() => startEdit(group)}>수정</Button>
          <Popconfirm title="기관 그룹을 삭제할까요?" onConfirm={() => deleteGroup(group.id)} okText="삭제" cancelText="취소">
            <Button type="link" danger icon={<DeleteOutlined />}>삭제</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Modal open={open} onCancel={onClose} onOk={draft ? saveDraft : onClose} width={1180} okText={draft ? "저장" : "닫기"} cancelText="닫기" title="기관 범위 관리">
      <Alert
        type="info"
        showIcon
        message="기관 그룹은 나라장터의 공식 분류가 아니라, 사용자가 관리하는 수요기관 묶음입니다."
        description="공고명 키워드와 합치지 않고 수요기관명·기관코드·별칭·산하기관 목록만으로 판정합니다."
      />
      <div className="collection-modal-toolbar">
        <Text type="secondary">이 수집 설정에서 재사용하는 개인 관리 데이터입니다.</Text>
        <Button type="primary" icon={<PlusOutlined />} onClick={startCreate}>기관 그룹 추가</Button>
      </div>
      <Table rowKey="id" size="small" columns={columns} dataSource={groups} pagination={false} scroll={{ x: 1080 }} />
      {draft && (
        <Card size="small" className="collection-group-editor" title={editingId === "new" ? "새 기관 그룹" : `기관 그룹 수정: ${activeGroup?.name || ""}`}>
          <Row gutter={[12, 12]}>
            <Col xs={24} md={12}><Text strong>기관 그룹 이름</Text><Input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="예: 지역 교육기관" /></Col>
            <Col xs={24} md={12}><Text strong>대표기관</Text><Select mode="tags" value={draft.parentAgencies} onChange={(value) => setDraft({ ...draft, parentAgencies: value })} placeholder="대표 수요기관명을 입력" /></Col>
            <Col xs={24} md={12}><Text strong>산하기관</Text><Select mode="tags" value={draft.childAgencies} onChange={(value) => setDraft({ ...draft, childAgencies: value })} placeholder="산하기관명을 입력" /></Col>
            <Col xs={24} md={12}><Text strong>기관 별칭</Text><Select mode="tags" value={draft.aliases} onChange={(value) => setDraft({ ...draft, aliases: value })} placeholder="기관 별칭을 입력" /></Col>
            <Col xs={24} md={12}><Text strong>기관코드</Text><Select mode="tags" value={draft.codes} onChange={(value) => setDraft({ ...draft, codes: value })} placeholder="나라장터 기관코드를 입력" /></Col>
          </Row>
        </Card>
      )}
    </Modal>
  );
}

function CollectionSettingsEditor({ setting, onChange }) {
  return (
    <Card className="collection-condition-card">
        <Text strong>검색 조건</Text>
        <Row gutter={[16, 16]}>
          <Col xs={24} xl={6}>
            <div className="collection-field"><Text strong>업무 구분</Text><Select mode="multiple" allowClear value={setting.workTypes} onChange={(value) => onChange({ workTypes: value })} options={WORK_TYPE_OPTIONS.map((value) => ({ value, label: value }))} placeholder="나라장터 전체 업무 구분" /><Text type="secondary">선택하지 않으면 나라장터 공사·용역·물품 공고를 모두 조회합니다.</Text></div>
          </Col>
          <Col xs={24} xl={6}>
            <div className="collection-field"><Text strong>필수 키워드</Text><Select mode="tags" tokenSeparators={[","]} value={setting.requiredKeywords} onChange={(value) => onChange({ requiredKeywords: value })} placeholder="예: 연수, 교육" /><Text type="secondary">입력한 단어가 모두 들어간 공고를 찾습니다.</Text></div>
          </Col>
          <Col xs={24} xl={6}>
            <div className="collection-field"><Text strong>제외 키워드</Text><Select mode="tags" tokenSeparators={[","]} value={setting.excludedKeywords} onChange={(value) => onChange({ excludedKeywords: value })} placeholder="예: 시설, 공사, 유지보수" /><Text type="secondary">입력한 단어가 하나라도 있으면 결과에서 뺍니다.</Text></div>
          </Col>
          <Col xs={24} xl={6}><div className="collection-field"><Text strong>참가 가능 지역</Text><Select mode="multiple" allowClear value={setting.participationRegions} onChange={(value) => onChange({ participationRegions: value })} options={REGION_OPTIONS.map((value) => ({ value, label: value }))} placeholder="지역 제한 없음" /></div></Col>
        </Row>
        <Divider />
        <Row gutter={[16, 16]}>
          <Col xs={24} xl={12}><div className="collection-field no-top-margin"><Text strong>게시일자</Text><Space wrap><DatePicker value={setting.postedDateStart ? dayjs(setting.postedDateStart) : null} onChange={(value) => onChange({ postedDateStart: value ? value.format("YYYY-MM-DD") : "" })} placeholder="시작일" /><Text type="secondary">~</Text><DatePicker value={setting.postedDateEnd ? dayjs(setting.postedDateEnd) : null} onChange={(value) => onChange({ postedDateEnd: value ? value.format("YYYY-MM-DD") : "" })} placeholder="종료일" /></Space></div></Col>
          <Col xs={24} xl={12}><div className="collection-field no-top-margin"><Text strong>기초금액</Text><Space wrap><InputNumber min={0} value={setting.baseAmountMin} onChange={(value) => onChange({ baseAmountMin: value })} addonAfter="원" placeholder="최소 금액 (선택)" /><Text type="secondary">~</Text><InputNumber min={0} value={setting.baseAmountMax} onChange={(value) => onChange({ baseAmountMax: value })} addonAfter="원" placeholder="최대 금액 (선택)" /></Space></div></Col>
        </Row>
    </Card>
  );
}

function RegionRestrictionCell({ item }) {
  const restriction = item.common_storage_record?.region_restriction;
  if (restriction === true) return <Tag color="orange">제한 있음</Tag>;
  if (restriction === false) return <Tag color="green">제한 없음</Tag>;
  return <Tag color="gold">확인 필요</Tag>;
}

function NoticeDateCell({ publishedAt, closingAt }) {
  return <div className="collection-notice-date"><div>게시 {formatDeadline(publishedAt)}</div><div>입찰마감 {formatDeadline(closingAt)}</div></div>;
}

function publishedAtTimestamp(item) {
  const timestamp = Date.parse(item.published_at || "");
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function bidProgressState(item) {
  const stage = String(item?.progress_status || "").trim();
  const procedure = String(item?.detail_procedure || "").trim();
  const procedureStatus = String(item?.detail_procedure_status || "").trim();
  const isComplete = procedureStatus.includes("완료");
  const isInProgress = procedureStatus.includes("진행중");

  if (stage.includes("입찰공고")) return "입찰 진행";
  if (stage.includes("개찰")) return isComplete ? "개찰 완료" : "개찰 진행";
  if (stage.includes("적격심사")) {
    return isInProgress ? "적격심사 진행 중" : isComplete ? "적격심사 완료" : "적격심사";
  }
  if (stage.includes("제안서") || stage.includes("공모")) {
    return isComplete ? "제안서 제출 완료" : "제안서 심사·제출 단계";
  }
  if (stage.includes("낙찰자선정")) return isComplete ? "낙찰자 선정 완료" : "낙찰자 선정 진행 중";
  if (stage.includes("계약체결")) return isComplete ? "계약 체결 완료" : "계약 체결 진행 중";

  return [stage, procedure, procedureStatus].filter(Boolean).join(" · ") || "확인 필요";
}

function effectiveMatchStatus(item) {
  const industryRestriction = item?.industry_restriction;
  const label = String(industryRestriction?.label || "");
  // An industry-code mismatch is a hard exclusion regardless of the
  // preliminary keyword/date classification sent by any upstream source.
  if (industryRestriction?.state === "FAIL" || label.includes("불일치")) {
    return "EXCLUDE";
  }
  return item?.match_status || "REVIEW";
}

function EnrichmentCheckCell({ check }) {
  const color = {
    PASS: "green",
    ALLOWED: "green",
    NO_RESTRICTION: "blue",
    FAIL: "red",
    NOT_ALLOWED: "red",
    REVIEW: "gold",
  }[check?.state] || "default";
  const label = check?.label || "확인 전";
  const parenthesisIndex = label.indexOf("(");
  const displayLabel = parenthesisIndex > 0 ? <>{label.slice(0, parenthesisIndex).trim()}<br />{label.slice(parenthesisIndex)}</> : label;
  return <div className="collection-enrichment-cell"><Tooltip title={check?.evidence?.join("\n") || "아직 상세 확인하지 않음"}><Tag className="collection-enrichment-tag" color={color}>{displayLabel}</Tag></Tooltip></div>;
}

function AttachmentCell({ attachments, sources, label }) {
  const downloadSources = (sources || []).filter((source) => source?.download_url);
  const sourceLinks = (
    <Space direction="vertical" size={6} className="collection-attachment-links">
      {downloadSources.map((source, index) => (
        <a key={`${source.download_url}-${index}`} href={source.download_url} target="_blank" rel="noreferrer">
          {index + 1}. {source.file_name || `첨부파일 ${index + 1}`}
        </a>
      ))}
    </Space>
  );
  if (attachments?.length || downloadSources.length) {
    return (
      <Space direction="vertical" size={2}>
        {attachments?.length ? <Tooltip title={attachments.map((attachment) => `${attachment.file_name}\n${attachment.local_path}\n${attachment.extraction_message}`).join("\n\n")}><Tag color="blue">{attachments.length}개 분석</Tag></Tooltip> : <Tag color="cyan">{downloadSources.length}개 확인</Tag>}
        {downloadSources.length ? <Popover title="나라장터 첨부파일" content={sourceLinks} trigger="click"><Button type="link" size="small">첨부 링크 {downloadSources.length}</Button></Popover> : null}
      </Space>
    );
  }
  return <Text type="secondary">{label || "확인 전"}</Text>;
}

function ResultsSection({
  setting,
  preview,
  loading,
  saving,
  selectedRecordIds,
  onPreview,
  testMode,
  onTestModeChange,
  onSelectedChange,
  onSaveSelected,
}) {
  const rows = preview?.items || [];
  const summary = preview?.summary;
  const [resultTab, setResultTab] = useState("ALL");
  const resultTabs = [
    { key: "ALL", label: `전체 ${rows.length}` },
    { key: "PRIORITY", label: `우선 검토 ${rows.filter((row) => effectiveMatchStatus(row) === "PRIORITY").length}` },
    { key: "REVIEW", label: `확인 필요 ${rows.filter((row) => effectiveMatchStatus(row) === "REVIEW").length}` },
    { key: "EXCLUDE", label: `제외 ${rows.filter((row) => effectiveMatchStatus(row) === "EXCLUDE").length}` },
  ];
  const visibleRows = resultTab === "ALL" ? rows : rows.filter((row) => effectiveMatchStatus(row) === resultTab);
  const columns = [
    { title: "번호", key: "row_number", width: 68, render: (_, __, index) => index + 1 },
    Table.SELECTION_COLUMN,
    { title: "공고명", dataIndex: "business_name", width: 280, ellipsis: true },
    { title: "공고번호", dataIndex: "bid_notice_no", width: 145 },
    { title: "업무구분", dataIndex: "work_type", width: 100, render: (value) => value || "확인 필요" },
    {
      title: "게시일시 / 입찰마감일시",
      key: "published_at",
      width: 175,
      sorter: (left, right) => publishedAtTimestamp(left) - publishedAtTimestamp(right),
      defaultSortOrder: "descend",
      sortDirections: ["descend", "ascend"],
      render: (_, item) => <NoticeDateCell publishedAt={item.published_at} closingAt={item.bid_closing_at} />,
    },
    { title: "수요기관", dataIndex: "demand_agency_name", width: 180, ellipsis: true },
    { title: "입찰 진행 상태", key: "bid_progress_state", width: 155, render: (_, item) => bidProgressState(item) },
    { title: "사업금액", dataIndex: "business_amount", width: 145, render: formatAmount },
    { title: "업종제한(기관코드)", width: 185, render: (_, item) => <EnrichmentCheckCell check={item.industry_restriction} /> },
    { title: "공동도급", width: 140, render: (_, item) => <EnrichmentCheckCell check={item.joint_contracting} /> },
    { title: "지역제한", width: 165, render: (_, item) => <EnrichmentCheckCell check={item.region_restriction_detail} /> },
    { title: "원문", dataIndex: "source_url", width: 90, render: (value) => value ? <a href={value} target="_blank" rel="noreferrer">열기</a> : "—" },
    { title: "첨부파일", width: 145, render: (_, item) => <AttachmentCell attachments={item.attachments} sources={item.attachment_sources} label={item.attachment_lookup_label} /> },
  ];
  const selectedRows = rows.filter((row) => selectedRecordIds.includes(row.record_id));
  const rowSelection = {
    selectedRowKeys: selectedRecordIds,
    onChange: onSelectedChange,
    getCheckboxProps: (row) => ({
      disabled: effectiveMatchStatus(row) === "EXCLUDE",
      name: row.business_name || row.bid_notice_no || "입찰공고",
    }),
  };

  return (
    <section className="collection-results-section">
      <div className="collection-results-heading">
        <div><Text className="collection-section-label">실제 수집 미리보기</Text><Title level={2}>수집 결과</Title><Paragraph type="secondary">나라장터 목록을 조회한 뒤, 표시되는 모든 공고의 상세 페이지와 첨부파일까지 분석합니다. 내용을 확인하고 체크한 공고만 Sheet에 추가합니다.</Paragraph></div>
        <Space wrap>
          <Tooltip title="테스트 모드는 최신 10건만 가져오며, 가져온 10건 모두 상세 페이지와 첨부파일까지 분석합니다."><Space size={6}><Text type="secondary">테스트 조회</Text><Switch size="small" checked={testMode} onChange={onTestModeChange} checkedChildren="10건" unCheckedChildren="전체" /></Space></Tooltip>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={onPreview}>나라장터 조회</Button>
          <Button type="primary" icon={<SaveOutlined />} disabled={!selectedRows.length} loading={saving} onClick={() => onSaveSelected(selectedRows)}>선택 {selectedRows.length}건 Sheets 저장</Button>
        </Space>
      </div>
      {summary && <div className="collection-preview-summary"><Tag color="blue">조회 {summary.fetched_count}건</Tag><Tag color="green">우선 검토 {summary.priority_count}건</Tag><Tag color="gold">확인 필요 {summary.review_count}건</Tag><Tag>제외 {summary.exclude_count}건</Tag></div>}
      {rows.length ? <><Tabs className="collection-result-tabs" activeKey={resultTab} onChange={setResultTab} items={resultTabs} /><Table className="collection-result-table" rowKey="record_id" rowSelection={rowSelection} loading={loading} columns={columns} dataSource={visibleRows} pagination={{ pageSize: 20, hideOnSinglePage: true }} scroll={{ x: 2845 }} /></> : <Empty description={loading ? "나라장터 공고를 조회하고 있습니다." : "나라장터 조회 버튼을 눌러 실제 수집 결과를 확인하세요."} />}
    </section>
  );
}

function createEmptySetting() {
  return {
    id: `setting-${Date.now()}`,
    name: "새 수집 설정",
    memo: "",
    workTypes: [],
    requiredKeywords: [],
    excludedKeywords: [],
    baseAmountMin: null,
    baseAmountMax: null,
    participationRegions: [],
    postedDateStart: "",
    postedDateEnd: "",
    recipients: [],
    instantAlert: true,
    digestTime: "17:30",
    sheet: SHEET_OPTIONS[0],
  };
}

function G2BCollectionSettingsPreview() {
  const [settings, setSettings] = useState(INITIAL_COLLECTION_SETTINGS);
  const [activeSettingId, setActiveSettingId] = useState(INITIAL_COLLECTION_SETTINGS[0].id);
  const [previewsBySetting, setPreviewsBySetting] = useState({});
  const [selectedRecordIdsBySetting, setSelectedRecordIdsBySetting] = useState({});
  const [isPreviewLoading, setPreviewLoading] = useState(false);
  const [isSheetSaving, setSheetSaving] = useState(false);
  const [isTestPreview, setTestPreview] = useState(true);
  const activeSetting = settings.find((setting) => setting.id === activeSettingId) || settings[0];
  const activePreview = previewsBySetting[activeSettingId];
  const activeSelectedRecordIds = selectedRecordIdsBySetting[activeSettingId] || [];

  const changeActiveSetting = (patch) => setSettings((current) => updateSetting(current, activeSetting.id, patch));
  const addSetting = () => {
    const next = createEmptySetting();
    setSettings((current) => [...current, next]);
    setActiveSettingId(next.id);
    message.success("새 수집 설정을 추가했습니다.");
  };
  const duplicateSetting = () => {
    const copy = { ...activeSetting, id: `setting-${Date.now()}`, name: `${activeSetting.name} 사본`, recipients: [...activeSetting.recipients], workTypes: [...(activeSetting.workTypes || [])], requiredKeywords: [...activeSetting.requiredKeywords], excludedKeywords: [...activeSetting.excludedKeywords], participationRegions: [...activeSetting.participationRegions] };
    setSettings((current) => [...current, copy]);
    setActiveSettingId(copy.id);
    message.success("수집 설정을 복제했습니다.");
  };
  const deleteSetting = (settingId) => {
    if (settings.length <= 1) return;
    const nextSettings = settings.filter((setting) => setting.id !== settingId);
    setSettings(nextSettings);
    if (settingId === activeSetting.id) setActiveSettingId(nextSettings[0].id);
    message.success("수집 설정을 삭제했습니다.");
  };
  const saveWorkspace = () => {
    message.success("이 브라우저에 임시 저장되었습니다.");
  };
  const previewNotices = async () => {
    setPreviewLoading(true);
    try {
      const preview = await fetchBidNoticePreview(activeSetting, isTestPreview ? 10 : null);
      setPreviewsBySetting((current) => ({ ...current, [activeSetting.id]: preview }));
      setSelectedRecordIdsBySetting((current) => ({ ...current, [activeSetting.id]: [] }));
      message.success(`나라장터 공고 ${preview.summary.fetched_count}건을 조회했습니다.`);
    } catch (error) {
      message.error(error.message || "나라장터 공고를 조회하지 못했습니다.");
    } finally {
      setPreviewLoading(false);
    }
  };
  const changeSelectedRecordIds = (recordIds) => {
    setSelectedRecordIdsBySetting((current) => ({ ...current, [activeSetting.id]: recordIds }));
  };
  const saveSelectedNotices = async (selectedItems) => {
    setSheetSaving(true);
    try {
      const result = await saveSelectedBidNotices(activeSetting.name, selectedItems);
      setSelectedRecordIdsBySetting((current) => ({ ...current, [activeSetting.id]: [] }));
      const skipped = Number(result.skipped_duplicate_count || 0);
      message.success(`${result.saved_count}건을 Google Sheets에 저장했습니다.${skipped ? ` 이미 저장된 공고 ${skipped}건은 건너뛰었습니다.` : ""}`);
    } catch (error) {
      message.error(error.message || "Google Sheets에 저장하지 못했습니다.");
    } finally {
      setSheetSaving(false);
    }
  };

  if (!activeSetting) return null;

  return (
    <main className="collection-preview">
      <header className="collection-header">
        <div><Text className="collection-eyebrow">G2B OPPORTUNITY MANAGEMENT</Text><Title>입찰공고 수집</Title><Paragraph>키워드와 날짜 조건으로 나라장터 공고를 찾아 확인합니다.</Paragraph></div>
      </header>

      <div className="collection-main-content">
        <DeliverySettingsPanel setting={activeSetting} onChange={changeActiveSetting} />
        <CollectionSettingsEditor setting={activeSetting} onChange={changeActiveSetting} />
        <ResultsSection
          setting={activeSetting}
          preview={activePreview}
          loading={isPreviewLoading}
          saving={isSheetSaving}
          testMode={isTestPreview}
          onTestModeChange={setTestPreview}
          selectedRecordIds={activeSelectedRecordIds}
          onPreview={previewNotices}
          onSelectedChange={changeSelectedRecordIds}
          onSaveSelected={saveSelectedNotices}
        />
      </div>
    </main>
  );
}

export default G2BCollectionSettingsPreview;
