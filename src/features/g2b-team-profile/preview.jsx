import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import "antd/dist/reset.css";
import G2BCollectionSettingsPreview from "./G2BCollectionSettingsPreview";

ReactDOM.createRoot(document.getElementById("root")).render(
  <ConfigProvider
    theme={{
      token: {
        colorPrimary: "#3182f6",
        colorText: "#191f28",
        colorTextSecondary: "#4e5968",
        colorBorder: "#e5e8eb",
        colorBgLayout: "#f6f5f4",
        borderRadius: 10,
        fontFamily: '"Pretendard Variable", Pretendard, "Noto Sans KR", sans-serif',
      },
      components: {
        Card: { borderRadiusLG: 18 },
        Table: { headerBg: "#f7f8fa", headerColor: "#4e5968" },
      },
    }}
  >
    <G2BCollectionSettingsPreview />
  </ConfigProvider>,
);
