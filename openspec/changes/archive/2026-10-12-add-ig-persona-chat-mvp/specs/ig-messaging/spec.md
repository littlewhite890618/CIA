## Purpose

提供與 Instagram 私訊平台之間的雙向通道，涵蓋事件接收與驗證、角色路由、訊息發送，並在系統層強制執行 Meta 平台的訊息視窗、長度與速率限制，使上層功能不需個別處理平台規則。

## ADDED Requirements

### Requirement: Webhook 簽章驗證

系統 SHALL 對每一個進入的 Instagram webhook 請求驗證 `X-Hub-Signature-256` 標頭，並 MUST 在驗證失敗時拒絕該請求且不進行任何後續處理。

#### Scenario: 簽章有效

- **WHEN** 收到帶有正確 `X-Hub-Signature-256` 標頭的 webhook 請求
- **THEN** 系統接受該請求並回應 HTTP 200，事件進入後續處理流程

#### Scenario: 簽章無效或缺漏

- **WHEN** 收到的 webhook 請求其簽章與 payload 不符，或完全缺少 `X-Hub-Signature-256` 標頭
- **THEN** 系統拒絕該請求、回應 HTTP 403，且不建立任何 session、不寫入任何訊息、不觸發任何模型呼叫

#### Scenario: Webhook 訂閱驗證

- **WHEN** Meta 以 `hub.mode=subscribe` 與 `hub.verify_token` 發出訂閱驗證請求，且 token 與設定值相符
- **THEN** 系統原樣回傳 `hub.challenge` 值

### Requirement: 事件路由至角色

系統 SHALL 依 webhook payload 中的 `entry[].id`（Instagram 專業帳號 ID）將事件路由至對應的角色設定，並 MUST 在收到未知帳號 ID 的事件時記錄並安全略過，而非中斷整批事件的處理。

#### Scenario: 已知角色帳號

- **WHEN** webhook 事件的 `entry[].id` 對應到一個已啟用的角色
- **THEN** 該事件以該角色的設定進行處理

#### Scenario: 未知角色帳號

- **WHEN** webhook 事件的 `entry[].id` 不對應任何已設定的角色
- **THEN** 系統記錄警告並略過該筆事件，同批次中其他可辨識的事件仍正常處理，且整體請求仍回應 HTTP 200

#### Scenario: 單一請求含多筆事件

- **WHEN** 單一 webhook 請求的 payload 包含多筆 `entry` 或多筆 `messaging` 項目
- **THEN** 系統逐筆處理每一項，任一項處理失敗不影響其餘項目

### Requirement: 24 小時訊息視窗強制檢查

系統 SHALL 為每一組使用者與角色的對話維護訊息視窗到期時間，並 MUST 在送出任何訊息前檢查該時間。視窗 SHALL 於使用者每次送出訊息、或每次觸發 postback 時重置為 24 小時。

#### Scenario: 視窗內發訊

- **WHEN** 系統要送出訊息，且該對話的視窗到期時間尚未過期
- **THEN** 訊息正常送出

#### Scenario: 視窗已逾期

- **WHEN** 系統要送出訊息，但該對話的視窗到期時間已過
- **THEN** 系統不呼叫 Instagram 發訊 API，將該訊息標記為未送達並記錄原因

#### Scenario: 使用者發訊重置視窗

- **WHEN** 使用者送出一則訊息
- **THEN** 該對話的視窗到期時間更新為該訊息時間起算的 24 小時後

#### Scenario: Postback 重置視窗

- **WHEN** 使用者點擊按鈕而觸發 `messaging_postbacks` 事件
- **THEN** 該對話的視窗到期時間更新為該事件時間起算的 24 小時後

### Requirement: 禁止對未互動使用者主動發訊

系統 MUST NOT 對從未向該角色帳號送出過訊息或觸發過 postback 的 Instagram 使用者發送訊息。

#### Scenario: 無互動紀錄

- **WHEN** 系統被要求對一個沒有任何入站互動紀錄的使用者發訊
- **THEN** 系統拒絕該發訊要求並記錄原因，不呼叫 Instagram 發訊 API

### Requirement: 訊息長度切分與順序保證

系統 SHALL 將超過 1000 bytes（UTF-8 編碼）的訊息內容切分為多則訊息後依序送出，切分 MUST NOT 破壞多位元組字元，且多則訊息 MUST 依原始順序抵達。

#### Scenario: 內容超過長度上限

- **WHEN** 要送出的訊息內容其 UTF-8 編碼超過 1000 bytes
- **THEN** 系統將其切分為多則各自不超過 1000 bytes 的訊息，並依序送出

#### Scenario: 切分點落在多位元組字元上

- **WHEN** 切分位置正好落在一個中文字或 emoji 的位元組中間
- **THEN** 系統調整切分點至該字元邊界，送出的每一則訊息皆為合法的 UTF-8 且無亂碼

#### Scenario: 多則訊息的順序

- **WHEN** 一則內容被切分為三則訊息送出
- **THEN** 使用者依序收到第一、第二、第三則，順序不錯亂

### Requirement: 發訊速率配額

系統 SHALL 為每個角色帳號維護滾動時間窗內的發訊計數，並 MUST 在達到設定上限時暫緩發訊而非直接失敗。上限 SHALL 為可調設定值。

#### Scenario: 配額充足

- **WHEN** 角色帳號在當前時間窗內的發訊數未達上限
- **THEN** 訊息立即送出，計數加一

#### Scenario: 達到配額上限

- **WHEN** 角色帳號在當前時間窗內的發訊數已達上限
- **THEN** 系統暫緩該則訊息並於配額釋出後重試，不丟棄訊息

#### Scenario: 平台回傳速率限制錯誤

- **WHEN** Instagram 發訊 API 回傳速率限制相關錯誤
- **THEN** 系統記錄該事件、退避後重試，並將實際觸發上限的時機與數量記錄下來以供分析

### Requirement: 對話中的輸入狀態提示

系統 SHALL 在產生角色回覆的等待期間，向使用者顯示 Instagram 的輸入中狀態提示。

#### Scenario: 產生回覆期間

- **WHEN** 系統收到使用者訊息並開始產生角色回覆
- **THEN** 系統送出輸入中狀態提示，並在回覆送出後結束該提示
