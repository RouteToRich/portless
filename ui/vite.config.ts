import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The UI talks to the combined dev server on port 8081.
// Every module is reachable at its own prefix (e.g. /api/deals/..., /api/trade-min/...).
// For microservices production mode set VITE_API_BASE_URL to the gateway URL.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8081",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
