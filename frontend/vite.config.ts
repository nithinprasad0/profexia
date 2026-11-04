import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");

  if (command === "build") {
    const apiUrl = (env.VITE_API_URL ?? "").trim();
    if (!apiUrl) {
      console.warn(
        "\n[profexia] VITE_API_URL is not set. The build will call the API on the same origin (/api/v1).\n" +
          "            Set VITE_API_URL to your backend URL (e.g. https://profexia-api.onrender.com) for Vercel.\n",
      );
    } else if (mode === "production" && /localhost|127\.0\.0\.1/.test(apiUrl)) {
      console.warn(`\n[profexia] VITE_API_URL points at ${apiUrl} in a production build.\n`);
    }
  }

  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: 5173,
    },
    preview: {
      port: 4173,
    },
  };
});
