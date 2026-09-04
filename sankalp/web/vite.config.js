import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxies /api/* to the FastAPI backend so the frontend never hardcodes a
// host — the same build works talking to localhost during dev.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/architecture": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
