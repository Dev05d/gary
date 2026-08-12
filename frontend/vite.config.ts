import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies to the backend so the browser only ever talks to one
// origin — no CORS in development, and the same relative URLs work in the
// production build that FastAPI serves from frontend/dist.
const BACKEND = process.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/ws": { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
