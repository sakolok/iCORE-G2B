import { Card, Empty, Typography } from "antd";
import "./PreSpecificationsPage.css";

function PreSpecificationsPage() {
  return (
    <section className="pre-specifications-page" aria-labelledby="pre-specifications-title">
      <header className="pre-specifications-hero">
        <span className="pre-specifications-eyebrow">나라장터 사전규격</span>
        <Typography.Title id="pre-specifications-title" level={2}>
          사전규격을 검토해요
        </Typography.Title>
        <Typography.Paragraph>
          공고 전 공개된 규격을 한곳에서 확인하고 검토할 수 있습니다.
        </Typography.Paragraph>
      </header>

      <Card className="pre-specifications-empty-card">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <div className="pre-specifications-empty-copy">
              <strong>아직 표시할 사전규격이 없습니다.</strong>
              <span>수집된 사전규격은 다음 연결 단계에서 이곳에 표시됩니다.</span>
            </div>
          }
        />
      </Card>
    </section>
  );
}

export default PreSpecificationsPage;
