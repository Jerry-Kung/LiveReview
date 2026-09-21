/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendPort = env.VITE_BACKEND_PORT || "12439";

  return {
    plugins: [react()],
    server: {
      proxy: {
        "/health": {
          target: `http://localhost:${backendPort}`,
          changeOrigin: true,
        },
        // 上传与任务接口同样转发到后端；分片请求体较大，因此放宽超时
        "/api": {
          target: `http://localhost:${backendPort}`,
          changeOrigin: true,
          timeout: 300_000,
        },
      },
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["src/test/setup.ts"],
    },
  };
});
