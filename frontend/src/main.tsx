import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { SettingsPage } from "./settings/SettingsPage";
import "./settings/settings.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <SettingsPage />
  </StrictMode>,
);
