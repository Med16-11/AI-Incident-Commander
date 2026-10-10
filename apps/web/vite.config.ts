import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": { target: env.OPSMIND_API_PROXY_TARGET || "http://localhost:8000", changeOrigin: true, rewrite: (path) => path.replace(/^\/api/, "") },
        "/remediation-api": { target: env.OPSMIND_REMEDIATION_PROXY_TARGET || "http://localhost:8003", changeOrigin: true, rewrite: (path) => path.replace(/^\/remediation-api/, "") },
        "/prometheus": { target: env.OPSMIND_PROMETHEUS_PROXY_TARGET || "http://localhost:9090", changeOrigin: true, rewrite: (path) => path.replace(/^\/prometheus/, "") }
      }
    }
  };
});
