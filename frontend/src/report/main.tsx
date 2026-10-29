import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ReportPage } from "./ReportPage";
// 與設定頁同一本活頁：沿用它的色彩、字體、紙張、封面帶、按鈕與提示頁
import "../settings/settings.css";
import "./report.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ReportPage />
  </StrictMode>,
);
