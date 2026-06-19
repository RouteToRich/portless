import axios from "axios";

// Single base URL: the FastAPI gateway acts as the BFF for the Ops Console.
// Falls back to the Vite dev proxy (/api) when VITE_API_BASE_URL is unset.
const baseURL = import.meta.env.VITE_API_BASE_URL ?? "/api";

export const api = axios.create({
  baseURL,
  timeout: 15000,
  headers: { "Content-Type": "application/json" },
});
