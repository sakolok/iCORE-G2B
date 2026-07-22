import { useEffect, useState } from "react";
import { Alert, Button, Spin } from "antd";
import LayoutShell from "./components/LayoutShell";
import LoginPage from "./pages/LoginPage";
import OpeningResultsPage from "./pages/OpeningResultsPage";
import PreSpecificationsPage from "./pages/PreSpecificationsPage";
import { authApi, AUTH_TOKEN_KEY, formatApiError } from "./api/client";

const LOCAL_SINGLE_USER_ENABLED =
  import.meta.env.DEV &&
  ["localhost", "127.0.0.1"].includes(window.location.hostname) &&
  ["1", "true", "yes", "on"].includes(
    String(import.meta.env.VITE_SINGLE_USER_MODE_ENABLED || "").trim().toLowerCase()
  );

const PAGE_BY_HASH = {
  "#pre-specifications": "pre-specifications",
  "#opening-results": "opening-results",
};

function pageFromHash() {
  return PAGE_BY_HASH[window.location.hash] || "opening-results";
}

function App() {
  const [session, setSession] = useState({ loading: true, error: "" });
  const [sessionAttempt, setSessionAttempt] = useState(0);
  const [activePage, setActivePage] = useState(pageFromHash);

  useEffect(() => {
    const handleHashChange = () => setActivePage(pageFromHash());
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  useEffect(() => {
    let active = true;

    const prepareSession = async () => {
      setSession({ loading: true, error: "" });
      const existingToken = window.localStorage.getItem(AUTH_TOKEN_KEY);

      if (existingToken) {
        try {
          const response = await authApi.me();
          if (!active) return;
          setSession({ ...response.data, token: existingToken, loading: false, error: "" });
          return;
        } catch {
          window.localStorage.removeItem(AUTH_TOKEN_KEY);
        }
      }

      if (!LOCAL_SINGLE_USER_ENABLED) {
        setSession({ loading: false, error: "" });
        return;
      }

      try {
        const response = await authApi.singleUserSession();
        if (!active) return;
        window.localStorage.setItem(AUTH_TOKEN_KEY, response.data.access_token);
        setSession({ ...response.data, token: response.data.access_token, loading: false, error: "" });
      } catch (error) {
        if (!active) return;
        setSession({
          loading: false,
          error: formatApiError(error, "기본 사용자 세션을 준비하지 못했습니다."),
        });
      }
    };

    prepareSession();

    return () => {
      active = false;
    };
  }, [sessionAttempt]);

  const handleLoginSuccess = (loginSession) => {
    setSession({
      ...loginSession,
      token: loginSession.access_token,
      loading: false,
      error: "",
    });
  };

  const handleLogout = () => {
    window.google?.accounts?.id?.disableAutoSelect?.();
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    setSession({ loading: false, error: "" });
  };

  if (session.loading) {
    return (
      <div className="app-session-loading" role="status" aria-live="polite">
        <div className="app-session-loading-content">
          <Spin size="large" />
          <span>업무 화면을 준비하고 있습니다.</span>
        </div>
      </div>
    );
  }

  if (session.error) {
    return (
      <div className="app-session-loading">
        <div className="app-session-loading-content is-error">
          <Alert
            type="error"
            showIcon
            message="업무 화면을 열 수 없습니다."
            description={session.error}
          />
          <Button type="primary" onClick={() => setSessionAttempt((current) => current + 1)}>
            다시 연결
          </Button>
        </div>
      </div>
    );
  }

  if (!session.token) {
    return <LoginPage onSuccess={handleLoginSuccess} />;
  }

  return (
    <LayoutShell
      session={session}
      activePage={activePage}
      onLogout={LOCAL_SINGLE_USER_ENABLED ? undefined : handleLogout}
    >
      {activePage === "pre-specifications" ? (
        <PreSpecificationsPage session={session} />
      ) : (
        <OpeningResultsPage />
      )}
    </LayoutShell>
  );
}

export default App;
