import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端打包後由 FastAPI 以同一個網址提供（同源，登入 cookie 不必跨網域）。
// 開發時 vite 跑在 5173，/api 轉給 FastAPI。
//
// ⚠️ 這台 Windows 主機的 Node 在專案路徑（含中文「專案」）下執行 vite、tsc 會直接當掉
// （存取違規，2026-10-15 實測；同樣的檔案放在英文路徑就正常）。因此開發伺服器與打包
// 一律在 Docker 的 node 容器裡跑，掛載的資料夾在 Windows 上需用輪詢偵測檔案變更。
const inContainer = process.env.VITE_IN_CONTAINER === "1";

export default defineConfig({
  plugins: [react()],
  server: {
    host: inContainer ? "0.0.0.0" : "localhost",
    port: 5173,
    watch: inContainer ? { usePolling: true, interval: 300 } : undefined,
    proxy: {
      "/api": process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
