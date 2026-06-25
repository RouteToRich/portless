import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// The UI talks to the combined dev server on port 8081.
// Every module is reachable at its own prefix (e.g. /api/deals/..., /api/trade-min/...).
// For microservices production mode set VITE_API_BASE_URL to the gateway URL.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      // Mock-mode fixtures live in the backend's documented mocked/ set.
      "@mocked": path.resolve(__dirname, "../backend/mocked"),
    },
  },
  server: {
    port: 5173,
    // Allow importing JSON fixtures from ../backend/mocked (outside the ui root).
    fs: {
      allow: [path.resolve(__dirname, ".."), path.resolve(__dirname)],
    },
    proxy: {
      "/api": {
        target: "http://localhost:8081",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  build: {
    target: "esnext",
  },
});
