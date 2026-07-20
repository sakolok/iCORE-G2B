import { useEffect, useState } from "react";
import {
  Button,
  Card,
  Form,
  Input,
  Select,
  Space,
  Switch,
  Table,
  TimePicker,
  Tooltip,
  Tag,
  message,
} from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { formatApiError, scraperApi } from "../api/client";
import "./ScraperControl.css";

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const normalizeTags = (items = []) => {
  const seen = new Set();
  const result = [];
  for (const item of items) {
    const cleaned = String(item || "").trim();
    if (!cleaned) {
      continue;
    }
    const key = cleaned.toLowerCase();
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    result.push(cleaned);
  }
  return result;
};

const normalizeNotifyTimes = (times = []) => {
  const seen = new Set();
  const result = [];
  for (const item of times) {
    const value = String(item || "").trim();
    if (!value) {
      continue;
    }
    const key = value.slice(0, 8);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    result.push(key);
  }
  return result;
};

function ScraperControl() {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [runHistory, setRunHistory] = useState([]);

  const loadRuns = async () => {
    try {
      const response = await scraperApi.listRuns(20);
      setRunHistory(response.data || []);
    } catch {
      setRunHistory([]);
    }
  };

  const loadConfig = async () => {
    setLoading(true);
    try {
      const response = await scraperApi.getConfig();
      const config = response.data;
      const configuredTimes = normalizeNotifyTimes(
        Array.isArray(config.notify_times) && config.notify_times.length > 0
          ? config.notify_times
          : [config.notify_time]
      );
      form.setFieldsValue({
        enabled: config.enabled,
        notify_times: configuredTimes.map((time) => dayjs(`2000-01-01T${time}`)),
        gsheet_ids: config.gsheet_ids || [],
        receiver_emails: normalizeTags(config.receiver_emails),
        keywords: normalizeTags(config.keywords),
      });
      setRunHistory(config.recent_runs || []);
    } catch (error) {
      message.error(formatApiError(error, "설정 조회에 실패했습니다."));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadConfig();
    loadRuns();
  }, []);

  const handleSave = async (values) => {
    try {
      const receiverEmails = normalizeTags(values.receiver_emails);
      const keywords = normalizeTags(values.keywords);

      const invalidEmail = receiverEmails.find((email) => !EMAIL_REGEX.test(email));
      if (invalidEmail) {
        message.error(`유효하지 않은 이메일 형식: ${invalidEmail}`);
        return;
      }

      const payload = {
        enabled: values.enabled,
        notify_times: normalizeNotifyTimes(
          (values.notify_times || []).map((item) => item?.format?.("HH:mm:ss") || "")
        ),
        gsheet_ids: normalizeTags(values.gsheet_ids),
        receiver_emails: receiverEmails,
        keywords,
      };
      const response = await scraperApi.updateConfig(payload);
      message.success(response.data.message);
      if (response.data.config?.recent_runs) {
        setRunHistory(response.data.config.recent_runs);
      }
    } catch (error) {
      message.error(formatApiError(error, "설정 저장에 실패했습니다."));
    }
  };

  const handleRunNow = async () => {
    try {
      const response = await scraperApi.trigger({ run_now: true, reason: "tool_ui_manual_run" });
      message.success(response.data.message);
      loadRuns();
    } catch (error) {
      message.error(formatApiError(error, "즉시 실행 요청에 실패했습니다."));
    }
  };

  const runColumns = [
    {
      title: "실행 시각",
      dataIndex: "executed_at",
      key: "executed_at",
      render: (value) => dayjs(value).format("YYYY-MM-DD HH:mm:ss"),
    },
    {
      title: "상태",
      dataIndex: "status",
      key: "status",
      render: (value) => {
        if (value === "success") {
          return <Tag color="green">성공</Tag>;
        }
        if (value === "partial") {
          return <Tag color="gold">부분성공</Tag>;
        }
        return <Tag color="red">실패</Tag>;
      },
    },
    { title: "수집", dataIndex: "notice_count", key: "notice_count" },
    { title: "중복제거", dataIndex: "deduped_count", key: "deduped_count" },
    { title: "메일발송", dataIndex: "email_sent_count", key: "email_sent_count" },
    { title: "시트기록", dataIndex: "sheet_written_count", key: "sheet_written_count" },
    {
      title: "메시지",
      dataIndex: "error_message",
      key: "error_message",
      ellipsis: true,
      render: (value) => value || "-",
    },
  ];

  return (
    <div className="scraper-control-page">
      <Card
        title="G2B 나라장터 수집기 제어"
        extra={
          <Space>
            <Button type="primary" htmlType="submit" form="scraper-config-form" loading={loading}>
              설정 저장
            </Button>
            <Button onClick={handleRunNow}>즉시 실행</Button>
          </Space>
        }
      >
        <Form id="scraper-config-form" layout="vertical" form={form} onFinish={handleSave}>
          <Form.Item name="enabled" label="스크래퍼 활성화" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.List
            name="notify_times"
            rules={[
              {
                validator: async (_, value) => {
                  if (!Array.isArray(value) || value.length === 0) {
                    throw new Error("최소 1개의 알림 시간을 선택하세요.");
                  }
                },
              },
            ]}
          >
            {(fields, { add, remove }, { errors }) => (
              <Form.Item label="알림 시간">
                <Space direction="vertical" size={10} style={{ width: "100%" }}>
                  {fields.map((field) => (
                    <div key={field.key} className="notify-time-row">
                      <Form.Item
                        {...field}
                        style={{ marginBottom: 0 }}
                        rules={[{ required: true, message: "시간을 선택하세요." }]}
                      >
                        <TimePicker format="HH:mm:ss" />
                      </Form.Item>
                      <Button danger type="text" onClick={() => remove(field.name)}>
                        삭제
                      </Button>
                    </div>
                  ))}
                  <Button
                    type="default"
                    className="notify-time-add-button"
                    onClick={() => add(dayjs("2000-01-01T09:00:00"))}
                    style={{ width: 44 }}
                  >
                    +
                  </Button>
                  <Form.ErrorList errors={errors} />
                </Space>
              </Form.Item>
            )}
          </Form.List>
          <Form.Item
            name="gsheet_ids"
            label={
              <Space size={6}>
                Google Sheet ID 목록
                <Tooltip
                  overlayInnerStyle={{
                    whiteSpace: "nowrap",
                    width: "max-content",
                    maxWidth: "90vw",
                  }}
                  title={
                    <>
                      구글시트 URL에서 /d/ 와 /edit 사이 문자열이 Sheet ID입니다.
                    </>
                  }
                >
                  <QuestionCircleOutlined />
                </Tooltip>
              </Space>
            }
          >
            <Select mode="tags" tokenSeparators={[","]} placeholder="예: 1AbCdEfGhIjKlMnOpQrStUvWxYz..." />
          </Form.Item>
          <Form.Item name="receiver_emails" label="수신 메일 목록" rules={[{ required: true }]}>
            <Select mode="tags" tokenSeparators={[","]} placeholder="mail1@company.com" />
          </Form.Item>
          <Form.Item name="keywords" label="키워드 목록" rules={[{ required: true }]}>
            <Select mode="tags" tokenSeparators={[","]} placeholder="AI 용역, 클라우드" />
          </Form.Item>
        </Form>

        <div className="scraper-runs-wrapper">
          <div className="scraper-runs-title">최근 실행 이력</div>
          <Table
            size="small"
            rowKey="run_id"
            dataSource={runHistory}
            columns={runColumns}
            pagination={{ pageSize: 8 }}
            scroll={{ x: 900 }}
          />
        </div>
      </Card>
    </div>
  );
}

export default ScraperControl;
