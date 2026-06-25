import axios from "axios";

// Single base URL: the FastAPI gateway acts as the BFF for the Ops Console.
// Falls back to the Vite dev proxy (/api) when VITE_API_BASE_URL is unset.
const baseURL = import.meta.env.VITE_API_BASE_URL ?? "/api";

// Mock mode: serve backend/mocked/ fixtures via a custom axios adapter so the
// whole console (System Health included) runs with no backend. Enable with
// VITE_USE_MOCKS=true (e.g. `npm run dev:mock`).
const useMocks = import.meta.env.VITE_USE_MOCKS === "true";

export const api = axios.create({
  baseURL,
  timeout: 15000,
  headers: { "Content-Type": "application/json" },
});

if (useMocks) {
  // Install a bootstrap adapter synchronously so the very first request (e.g.
  // the System Health poll) can't slip through to the network before the mock
  // module finishes loading. The lazy import keeps mocks out of prod bundles.
  const ready = import("@/mocks/adapter").then(({ mockAdapter }) => mockAdapter);
  api.defaults.adapter = async (config) => (await ready)(config);
  // eslint-disable-next-line no-console
  console.info("[portless] mock mode ON — serving backend/mocked fixtures");
}
