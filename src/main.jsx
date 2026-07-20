import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import "antd/dist/reset.css";
import "./App.css";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: "#3182f6",
          colorInfo: "#3182f6",
          colorSuccess: "#20a162",
          colorWarning: "#f59f00",
          colorError: "#e5484d",
          colorText: "#191f28",
          colorTextSecondary: "#4e5968",
          colorBorder: "#e5e8eb",
          colorBgLayout: "#f6f5f4",
          colorBgContainer: "#ffffff",
          borderRadius: 10,
          borderRadiusLG: 16,
          controlHeight: 40,
          fontFamily: '"Pretendard Variable", Pretendard, "Noto Sans KR", sans-serif',
        },
        components: {
          Button: {
            primaryShadow: "none",
            fontWeight: 600,
          },
          Card: {
            headerBg: "#ffffff",
          },
          Table: {
            headerBg: "#f7f8fa",
            headerColor: "#4e5968",
            rowHoverBg: "#f7faff",
            borderColor: "#edf0f2",
          },
        },
      }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>
);
