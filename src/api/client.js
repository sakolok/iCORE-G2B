import axios from "axios";

function getDefaultApiBaseUrl() {
  if (typeof window === "undefined") {
    return "http://localhost:8000/api";
  }

  const isLocalhost = ["localhost", "127.0.0.1"].includes(window.location.hostname);
  if (isLocalhost) {
    return "http://localhost:8000/api";
  }

  // 배포 환경에서는 동일 오리진 + /api 경로를 기본값으로 사용
  return `${window.location.origin}/api`;
}

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL || getDefaultApiBaseUrl();

function normalizeApiBaseUrl(rawUrl) {
  if (typeof window === "undefined") {
    return rawUrl;
  }

  try {
    const parsed = new URL(rawUrl, window.location.origin);

    // HTTPS 페이지에서는 HTTP API 호출이 브라우저에서 차단되므로 자동 보정
    if (window.location.protocol === "https:" && parsed.protocol === "http:") {
      parsed.protocol = "https:";
    }

    return parsed.toString().replace(/\/+$/, "");
  } catch {
    return rawUrl;
  }
}

export const API_BASE_URL = normalizeApiBaseUrl(configuredApiBaseUrl);

/** FastAPI 422 등에서 detail이 문자열·객체 배열·단일 객체일 때 안전한 메시지 문자열로 변환 */
export function formatApiError(error, fallback = "요청에 실패했습니다.") {
  const detail = error?.response?.data?.detail;
  if (detail == null || detail === "") {
    return fallback;
  }
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    const lines = detail
      .map((item) => {
        if (item == null) return "";
        if (typeof item === "string") return item;
        if (typeof item.msg === "string") {
          const loc = Array.isArray(item.loc)
            ? item.loc.filter((p) => p !== "body").join(".")
            : "";
          return loc ? `${loc}: ${item.msg}` : item.msg;
        }
        return "";
      })
      .filter(Boolean);
    return lines.length ? lines.join("\n") : fallback;
  }
  if (typeof detail === "object" && typeof detail.msg === "string") {
    return detail.msg;
  }
  if (typeof detail === "object" && typeof detail.message === "string") {
    return detail.message;
  }
  try {
    return JSON.stringify(detail);
  } catch {
    return fallback;
  }
}

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 10000,
});

let singleUserSessionPromise;

export const AUTH_TOKEN_KEY = "icore_admin_access_token";

api.interceptors.request.use((config) => {
  const token = window.localStorage.getItem(AUTH_TOKEN_KEY);
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (
      error?.response?.status === 401 &&
      typeof window !== "undefined" &&
      window.localStorage.getItem(AUTH_TOKEN_KEY)
    ) {
      window.localStorage.removeItem(AUTH_TOKEN_KEY);
      if (error?.config?.url !== "/auth/me") {
        window.location.reload();
      }
    }
    return Promise.reject(error);
  }
);

export const authApi = {
  login: (payload) => api.post("/auth/login", payload),
  googleLogin: (credential) => api.post("/auth/google", { credential }),
  singleUserSession: () => {
    if (!singleUserSessionPromise) {
      singleUserSessionPromise = api
        .post("/auth/single-user")
        .finally(() => {
          singleUserSessionPromise = undefined;
        });
    }
    return singleUserSessionPromise;
  },
  me: () => api.get("/auth/me"),
};

export const scraperApi = {
  getConfig: () => api.get("/scraper/config"),
  updateConfig: (payload) => api.put("/scraper/config", payload),
  trigger: (payload) => api.post("/scraper/trigger", payload),
  listRuns: (limit = 20) => api.get(`/scraper/runs?limit=${limit}`),
};

export const openingResultsApi = {
  list: (params) => api.get("/v1/results", { params }),
  detail: (resultId) => api.get(`/v1/results/${resultId}`),
  listArchive: (params) => api.get("/v1/results/archive", { params }),
  archiveDetail: (resultId) => api.get(`/v1/results/archive/${resultId}`),
  dismiss: (resultId) => api.delete(`/v1/results/${resultId}`),
  restore: (resultId) => api.post(`/v1/results/${resultId}/restore`),
  settings: () => api.get("/v1/results/settings"),
  updateProfile: (payload) => api.put("/v1/results/settings/profile", payload),
  listSheetDestinations: () => api.get("/v1/results/sheet-destinations"),
  verifySheetDestination: (payload) =>
    api.post("/v1/results/sheet-destinations/verify", payload, { timeout: 30000 }),
  saveSheetDestination: (payload) => api.post("/v1/results/sheet-destinations", payload),
  deleteSheetDestination: (destinationId) =>
    api.delete(`/v1/results/sheet-destinations/${destinationId}`),
  exportSheet: (payload) =>
    api.post("/v1/results/export/sheet", payload, { timeout: 60000 }),
};

export const preSpecificationsApi = {
  list: (params) => api.get("/v1/pre-specifications", { params }),
  detail: (registrationNumber) =>
    api.get(`/v1/pre-specifications/${encodeURIComponent(registrationNumber)}`),
  listArchive: (params) => api.get("/v1/pre-specifications/archive", { params }),
  archiveDetail: (registrationNumber) =>
    api.get(`/v1/pre-specifications/archive/${encodeURIComponent(registrationNumber)}`),
  collect: (payload) =>
    api.post("/v1/pre-specifications/collect", payload, { timeout: 60000 }),
  dismiss: (registrationNumber) =>
    api.delete(`/v1/pre-specifications/${encodeURIComponent(registrationNumber)}`),
  restore: (registrationNumber) =>
    api.post(`/v1/pre-specifications/${encodeURIComponent(registrationNumber)}/restore`),
  exportSheet: (payload) =>
    api.post("/v1/pre-specifications/export/sheet", payload, { timeout: 60000 }),
};
