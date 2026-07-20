import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Spin, Typography } from "antd";
import { API_BASE_URL, authApi, AUTH_TOKEN_KEY, formatApiError } from "../api/client";
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
          Math.min(360, Math.floor(googleButtonRef.current.clientWidth || 360))
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
      <header className="login-topbar">
        <div className="login-brand" aria-label="iCore">
          <span className="login-brand-mark" aria-hidden="true">i</span>
          <strong>iCore</strong>
        </div>
        <span className="login-topbar-note">회사 전용 워크스페이스</span>
      </header>

      <div className="login-frame">
        <section className="login-intro" aria-labelledby="login-product-title">
          <div className="login-intro-copy">
            <Typography.Text className="login-eyebrow">개찰결과 검토함</Typography.Text>
            <Typography.Title id="login-product-title" level={1}>
              필요한 개찰결과만
              <br />
              골라서 기록해요
            </Typography.Title>
            <Typography.Paragraph>
              내 키워드에 맞는 최근 결과를 확인하고,
              선택한 항목만 개인 Google Sheets에 반영해요.
            </Typography.Paragraph>
          </div>

          <ol className="login-flow" aria-label="업무 흐름">
            <li><span>1</span><strong>키워드로 모아요</strong><small>포함·제외 조건으로 필요한 결과만 보여줘요.</small></li>
            <li><span>2</span><strong>직접 확인해요</strong><small>공고 정보와 상위 업체 점수를 함께 확인해요.</small></li>
            <li><span>3</span><strong>고른 결과만 반영해요</strong><small>미리보기를 확인한 뒤 내 Sheet에 기록해요.</small></li>
          </ol>

          <p className="login-intro-footnote">12시간마다 새 결과를 모으고 최근 14일 동안 보여줘요.</p>
        </section>

        <section className="login-access" aria-labelledby="login-access-title">
          <div>
            <Typography.Text className="login-access-kicker">로그인</Typography.Text>
            <Typography.Title id="login-access-title" level={2}>업무 계정으로 시작해요</Typography.Title>
            <Typography.Paragraph>
              회사 Google Workspace 계정만 사용할 수 있어요.
            </Typography.Paragraph>
          </div>

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

          <div className="login-security-note">
            <strong>선택한 결과만 외부로 기록해요.</strong>
            <p>목록을 보거나 새로고침할 때는 Google Sheets를 호출하지 않아요.</p>
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
