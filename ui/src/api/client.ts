import axios from "axios";

// Single base URL: the FastAPI gateway acts as the BFF for the Ops Console.
// Falls back to the Vite dev proxy (/api) when VITE_API_BASE_URL is unset.
const baseURL = import.meta.env.VITE_API_BASE_URL ?? "/api";

// Mock mode: serve backend/mocked/ fixtures via a custom axios adapter so the
// whole console (System Health included) runs with no backend. Enable with
// VITE_USE_MOCKS=true (e.g. `npm run dev:mock`).
const useMocks = import.meta.env.VITE_USE_MOCKS === "true";

export const TOKEN_KEY = "portless_token";
export const USER_KEY = "portless_user";

export const api = axios.create({
  baseURL,
  // RAG queries (embed -> hybrid search -> rerank -> LLM generate) and Mongo
  // fallback retries can legitimately run well past 15s, so give every
  // request up to 2 minutes rather than aborting client-side.
  timeout: 120000,
  headers: { "Content-Type": "application/json" },
});

// Attach the JWT to every outgoing request.
api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) {
    config.headers = config.headers ?? {};
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// On 401 (expired/invalid token), clear the session and bounce to /login.
// Auth endpoints are exempt so login/register can surface their own errors.
api.interceptors.response.use(
  (res) => res,
  (error) => {
    const url: string = error.config?.url ?? "";
    if (error.response?.status === 401 && !url.includes("/auth/")) {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  }
);

if (useMocks) {
  // Install a bootstrap adapter synchronously so the very first request (e.g.
  // the System Health poll) can't slip through to the network before the mock
  // module finishes loading. The lazy import keeps mocks out of prod bundles.
  const ready = import("@/mocks/adapter").then(({ mockAdapter }) => mockAdapter);
  api.defaults.adapter = async (config) => (await ready)(config);
  // eslint-disable-next-line no-console
  console.info("[portless] mock mode ON — serving backend/mocked fixtures");
}
