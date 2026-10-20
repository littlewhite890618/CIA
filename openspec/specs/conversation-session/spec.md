# conversation-session Specification

## Purpose

管理使用者與角色之間對話的生命週期與記憶，包含 session 的建立與結束判定、使用者身分的跨角色歸戶，以及在不讓 context 無限成長的前提下維持對話連貫性。

## Requirements

### Requirement: 使用者身分歸戶

系統 SHALL 以 Instagram 使用者名稱（username）作為跨角色的使用者識別鍵，並 MUST 為每一組使用者與角色的組合分別保存該角色帳號範圍內的 IGSID。

#### Scenario: 使用者首次與某角色互動

- **WHEN** 一個尚無紀錄的 Instagram 使用者向某角色送出第一則訊息
- **THEN** 系統取得其使用者名稱、建立使用者紀錄，並建立該使用者與該角色之間的 IGSID 對應

#### Scenario: 同一使用者與第二個角色互動

- **WHEN** 一個已有紀錄的使用者向另一個角色送出訊息，因而產生一組不同的 IGSID
- **THEN** 系統依使用者名稱辨識出是同一人，將新的 IGSID 對應加入既有使用者之下，而非建立重複的使用者紀錄

#### Scenario: 無法取得使用者資料

- **WHEN** 系統因使用者已封鎖或其他原因無法取得其個人資料
- **THEN** 系統仍以該角色範圍內的 IGSID 建立可運作的對話紀錄，並將該使用者標記為身分未確認

### Requirement: Session 生命週期

系統 SHALL 將一段連續的互動歸入一個 session，並 MUST 在使用者靜默達設定時長後將該 session 標記為結束。靜默時長 SHALL 為可調設定值。

#### Scenario: 開啟新 session

- **WHEN** 使用者送出訊息，且該使用者與該角色之間不存在進行中的 session
- **THEN** 系統建立新的 session 並將該訊息歸入其中

#### Scenario: 延續進行中的 session

- **WHEN** 使用者送出訊息，且存在一個尚未結束的 session
- **THEN** 該訊息歸入既有 session，session 的最後活動時間更新

#### Scenario: 靜默達門檻

- **WHEN** 一個進行中的 session 其最後一則訊息距今已達設定的靜默時長
- **THEN** 系統將該 session 標記為結束，並記錄結束時間

#### Scenario: Session 結束後再次發訊

- **WHEN** 使用者在既有 session 已標記結束後又送出訊息
- **THEN** 系統建立一個新的 session，既有的已結束 session 保持不變

### Requirement: 對話記憶

系統 SHALL 在產生角色回覆時提供目前 session 的近期對話內容作為上下文，提供的訊息則數上限 MUST 為可調設定值，使上下文不隨對話無限成長。

#### Scenario: 短對話

- **WHEN** 目前 session 的訊息數未達上限
- **THEN** 該 session 完整的對話內容作為上下文提供

#### Scenario: 長對話超過上限

- **WHEN** 目前 session 的訊息數超過上限
- **THEN** 僅最近的訊息（則數等於上限）作為上下文提供，較早的訊息不送入模型

### Requirement: 跨 Session 的話題記憶

系統 SHALL 記錄每一組使用者與角色之間已談論過的話題，並 MUST 在新的 session 中將其提供給角色，使角色不重複詢問已問過的問題。

#### Scenario: 回訪使用者

- **WHEN** 一個先前談過工作與興趣的使用者開啟新的 session
- **THEN** 角色的上下文包含先前已談過的話題，角色不再重複詢問相同的基本問題

#### Scenario: 首次對話

- **WHEN** 使用者與該角色之間沒有任何先前的話題紀錄
- **THEN** 系統提供空的話題紀錄，角色以初次見面的方式互動

### Requirement: 訊息持久化

系統 SHALL 將每一則入站與出站訊息保存至資料庫，包含其方向、內容、平台訊息識別碼與時間。

#### Scenario: 收到使用者訊息

- **WHEN** 系統接收並通過驗證一則使用者訊息
- **THEN** 該訊息以入站方向被保存，並關聯至對應的 session

#### Scenario: 送出角色訊息

- **WHEN** 系統送出一則角色訊息
- **THEN** 該訊息以出站方向被保存，並關聯至對應的 session

#### Scenario: 重複的平台事件

- **WHEN** 系統收到一則平台訊息識別碼與既有紀錄相同的事件
- **THEN** 系統不重複保存該訊息，也不重複產生回覆
