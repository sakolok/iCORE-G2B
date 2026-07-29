import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Spin, Typography } from "antd";
import { API_BASE_URL, authApi, AUTH_TOKEN_KEY, formatApiError } from "../../api/client";
import "./LoginPage.css";

const GOOGLE_IDENTITY_SCRIPT_ID = "google-identity-services";
const GOOGLE_IDENTITY_SCRIPT_URL = "https://accounts.google.com/gsi/client";

let googleIdentityScriptPromise;

function loadGoogleIdentityScript() {
  if (window.google?.accounts?.id) {
    return Promise.resolve(window.google);
  }

  if (googleIdentityScriptPromise) {
    return googleIdentityScriptPromise;
  }

  googleIdentityScriptPromise = new Promise((resolve, reject) => {
    let script = document.getElementById(GOOGLE_IDENTITY_SCRIPT_ID);
    const isNewScript = !script;

    const handleLoad = () => {
      if (window.google?.accounts?.id) {
        resolve(window.google);
        return;
      }

      googleIdentityScriptPromise = undefined;
      reject(new Error("Google 로그인 모듈을 초기화하지 못했습니다."));
    };

    const handleError = () => {
      script?.remove();
      googleIdentityScriptPromise = undefined;
      reject(new Error("Google 로그인 모듈을 불러오지 못했습니다."));
    };

    if (isNewScript) {
      script = document.createElement("script");
      script.id = GOOGLE_IDENTITY_SCRIPT_ID;
      script.src = GOOGLE_IDENTITY_SCRIPT_URL;
      script.async = true;
      script.defer = true;
    }

    script.addEventListener("load", handleLoad, { once: true });
    script.addEventListener("error", handleError, { once: true });

    if (isNewScript) {
      document.head.appendChild(script);
    }
  });

  return googleIdentityScriptPromise;
}

function LoginPage({ onSuccess }) {
  const googleButtonRef = useRef(null);
  const [scriptAttempt, setScriptAttempt] = useState(0);
  const [googleReady, setGoogleReady] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const clientId = String(import.meta.env.VITE_GOOGLE_CLIENT_ID || "").trim();

  const handleCredential = useCallback(async (googleResponse) => {
    const credential = googleResponse?.credential;
    if (!credential) {
      setErrorMessage("Google에서 로그인 정보를 받지 못했습니다. 다시 시도해주세요.");
      return;
    }

    setSubmitting(true);
    setErrorMessage("");

    try {
      const response = await authApi.googleLogin(credential);
      window.localStorage.setItem(AUTH_TOKEN_KEY, response.data.access_token);
      onSuccess(response.data);
    } catch (error) {
      const isPotentialMixedContent =
        window.location.protocol === "https:" && String(API_BASE_URL).startsWith("http://");

      setErrorMessage(
        isPotentialMixedContent
          ? "보안 연결 문제로 로그인 요청이 차단되었습니다. 관리자에게 API 주소 확인을 요청해주세요."
          : formatApiError(error, "Google 로그인에 실패했습니다.")
      );
    } finally {
      setSubmitting(false);
    }
  }, [onSuccess]);

  useEffect(() => {
    if (!clientId) {
      setErrorMessage("Google 로그인 설정이 없습니다. 관리자에게 Client ID 설정을 요청해주세요.");
      return undefined;
    }

    let cancelled = false;
    setGoogleReady(false);
    setErrorMessage("");

    loadGoogleIdentityScript()
      .then((google) => {
        if (cancelled || !googleButtonRef.current) return;

        googleButtonRef.current.replaceChildren();
        google.accounts.id.initialize({
          client_id: clientId,
          callback: handleCredential,
          auto_select: false,
          cancel_on_tap_outside: true,
          ux_mode: "popup",
        });
        const googleButtonWidth = Math.max(
          200,
          Math.min(400, Math.floor(googleButtonRef.current.clientWidth || 400))
        );
        google.accounts.id.renderButton(googleButtonRef.current, {
          type: "standard",
          theme: "outline",
          size: "large",
          shape: "rectangular",
          text: "continue_with",
          logo_alignment: "left",
          width: googleButtonWidth,
          locale: "ko",
        });
        setGoogleReady(true);
      })
      .catch((error) => {
        if (!cancelled) {
          setErrorMessage(error.message || "Google 로그인 모듈을 불러오지 못했습니다.");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [clientId, handleCredential, scriptAttempt]);

  const retryGoogleScript = () => {
    setErrorMessage("");
    setScriptAttempt((current) => current + 1);
  };

  return (
    <main className="login-page">
      <div className="login-center">
        <section className="login-card">
          {/* ── Brand ── */}
          <div className="login-brand">
            <img
              src="/icore-logo.jpg"
              alt="iCORE Education & Consultancy"
              className="login-logo"
            />
            <Typography.Title level={1} className="login-title">
              G2B iCORE
            </Typography.Title>
            <Typography.Paragraph className="login-subtitle">
              나라장터 조달 정보를 한곳에서 정리하세요
            </Typography.Paragraph>
          </div>

          {/* ── Feature pills ── */}
          <div className="login-pills">
            <span className="login-pill">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
                <path d="M14 2v6h6" />
                <path d="M16 13H8" />
                <path d="M16 17H8" />
              </svg>
              사전규격
            </span>
            <span className="login-pill">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
                <path d="M14 2v4a2 2 0 0 0 2 2h4" />
                <circle cx="11.5" cy="14.5" r="2.5" />
                <path d="M13.3 16.3 15 18" />
              </svg>
              입찰공고
            </span>
            <span className="login-pill">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 20V10" />
                <path d="M18 20V4" />
                <path d="M6 20v-4" />
              </svg>
              개찰결과
            </span>
          </div>

          {/* ── Divider ── */}
          <div className="login-divider" />

          {/* ── Login section ── */}
          <div className="login-auth">
            <Typography.Text className="login-auth-label">업무 계정으로 시작하기</Typography.Text>

            <div className="login-domain-list" aria-label="허용 이메일 도메인">
              <span>@iceu.kr</span>
              <span>@iceu.co.kr</span>
            </div>

            {errorMessage ? (
              <Alert
                type="error"
                showIcon
                message="로그인을 진행할 수 없습니다."
                description={errorMessage}
                action={clientId ? <Button onClick={retryGoogleScript}>다시 시도</Button> : null}
              />
            ) : null}

            <div
              className={`login-google-stage${submitting ? " is-submitting" : ""}`}
              aria-busy={!googleReady || submitting}
            >
              {!googleReady ? (
                <div className="login-google-loading" role="status">
                  <Spin size="small" />
                  <span>Google 로그인을 준비하고 있습니다.</span>
                </div>
              ) : null}
              <div ref={googleButtonRef} className="login-google-button" />
              {submitting ? (
                <div className="login-submit-overlay" role="status">
                  <Spin size="small" />
                  <span>계정을 확인하고 있습니다.</span>
                </div>
              ) : null}
            </div>
          </div>

          <p className="login-policy">
            로그인하면 회사의 사용자 정책과 Google Workspace 인증 절차에 동의한 것으로 봐요.
          </p>
        </section>
      </div>
    </main>
  );
}

export default LoginPage;
