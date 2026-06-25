/**
 * Starts the Vite dev server programmatically.
 * This avoids spawning the esbuild native binary (blocked by Device Guard)
 * by passing config inline and disabling esbuild-dependent features.
 */
import { createServer } from "./node_modules/vite/dist/node/index.js";
import { createRequire } from "module";
const require = createRequire(import.meta.url);
const react = require("./node_modules/@vitejs/plugin-react/dist/index.cjs");
import { fileURLToPath } from "url";
import path from "path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

async function main() {
  const server = await createServer({
    root: __dirname,
    configFile: false,      // skip vite.config loading (avoids esbuild for .ts config)
    esbuild: false,         // disable esbuild for per-file transforms
    optimizeDeps: {
      noDiscovery: true,    // Vite 5.1+: disable dep pre-bundling (avoids esbuild.exe)
      include: [],
    },
    plugins: [
      react({
        // Force Babel mode for JSX/TSX transforms (no esbuild transform needed)
        babel: {
          babelrc: false,
          configFile: false,
          plugins: [],
        },
      }),
    ],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: "http://localhost:8081",
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ""),
        },
      },
    },
  });

  await server.listen();
  server.printUrls();
  console.log("\n  Press Ctrl+C to stop\n");
}

main().catch((err) => {
  console.error("Failed to start dev server:", err);
  process.exit(1);
});
