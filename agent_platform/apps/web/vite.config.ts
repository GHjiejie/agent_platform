import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target:
          loadEnv(mode, ".", "PLATFORM_").PLATFORM_API_PROXY ||
          "http://127.0.0.1:8000",
        // Preserve the browser-facing host so the API can validate Origin.
        changeOrigin: false,
      },
    },
  },
  build: { sourcemap: false },
}));
