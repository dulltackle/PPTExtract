import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const env = (globalThis as unknown as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};
const apiHost = env.PPTEXTRACT_API_HOST || "127.0.0.1";
const apiPort = env.PPTEXTRACT_API_PORT || "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": `http://${apiHost}:${apiPort}`,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
});
