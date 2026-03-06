import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: "127.0.0.1", // bind to IPv4 so browser and preview tools connect correctly
    // Proxy /api calls to a local Lambda dev server (sam local or direct invoke).
    // For quick local dev the frontend can also talk to the deployed API Gateway URL
    // by setting VITE_API_URL in .env.local.
    proxy: {
      "/api": {
        target: "http://localhost:3001",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
