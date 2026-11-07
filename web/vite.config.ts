import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs separately (`recompmanager serve` on :8080).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://localhost:8080" },
  },
});
