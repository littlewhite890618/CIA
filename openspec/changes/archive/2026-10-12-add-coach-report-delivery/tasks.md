<!-- 對應 PROGRESS.md「MVP」段落的 M5 教練報告生成 + M9 報告送達。
     零件多已存在（coach_reports 表、invite_sent 旗標、靜默輪詢、postback 解析、
     發訊管線），本 change 主要是串接與生成邏輯。 -->

## 1. 資料層

- [x] 1.1 `coach_reports` 新增失敗原因欄位，並登記至啟動補欄位機制
- [x] 1.2 實作報告的存取函式：依 session 查詢、建立、標記已生成、標記已送達、標記失敗
- [x] 1.3 實作「查詢已生成未送達的報告」，供補送流程使用
      2026-09-24：原本的 `repo.undelivered_reports_for_link()` 已隨決策 8
      移除——補救改為只由使用者再次點擊觸發（見 5.1），不需要「掃描這個
      使用者所有待補送報告」這個查詢；查單一 session 的報告已有
      `get_coach_report()`（1.2）足夠。

## 2. 邀請訊息

- [x] 2.1 實作邀請訊息的組裝，內含可攜帶 session 識別的結構化 postback payload
      一度改用 Quick Reply，2026-09-23 真機實測推翻後改回 **Button Template
      （postback）**——Quick Reply 在 Instagram 上 API 回 200 但按鈕根本不渲染
      （手機與桌面皆然），詳見 design.md 決策 0。
      `app/dialog/coach_report.py` 的 `build_invite_buttons()`／
      `parse_invite_payload()`；`app/channels/instagram.py` 新增
      `send_button_template()`。`_parse_one()` 不需改動，沿用既有 postback 分支。
- [x] 2.2 靜默輪詢在標記 session 結束後送出邀請，並將 `invite_sent` 設為已發送
      `app/main.py` 的 `_send_coach_invite()`，接在 `_close_session_and_analyze()`
      話題萃取／訊號偵測之後呼叫。
- [x] 2.3 實作互動過少的判定：使用者訊息數未達門檻則不邀請並記錄原因，門檻為可調設定
      新設定 `coach_report_min_user_messages`（預設 4）；不足時呼叫
      `repo.mark_invite_skipped(reason="too_few_messages")`。
- [x] 2.4 視窗逾期時不呼叫平台 API，標記未能邀請並記錄原因
      不另寫檢查，沿用 `MessageSender.send_quick_reply()` 的既有前置檢查
      （design.md 決策 6）；失敗時同樣呼叫 `mark_invite_skipped()`。
- [x] 2.5 測試：同一 session 重複輪詢僅邀請一次；訊息過少不邀請；逾期不呼叫 API
      `tests/test_coach_report.py`（整合，3 項）＋ `tests/test_sender.py`
      的 `send_quick_reply` 專用三項（視窗內送出／逾期不呼叫／未互動不發送）。

## 3. 報告生成

- [x] 3.1 建立報告生成模組，以該 session 的行為訊號為輸入，走 coaching 路徑
      `app/dialog/coach_report.py` 的 `generate_report()`。刻意不重新讀取原始
      逐則訊息——訊號的 reason 欄位在偵測當下已引用使用者原句，訊號本身
      就是「唯一資料來源」。
- [x] 3.2 設計並撰寫報告生成的提示，產出摘要／做得好／可調整／改寫示範／下次可試五段結構
      `build_system_prompt()`。
- [x] 3.3 解析模型輸出為結構化內容；解析失敗視為生成失敗，不留半成品
      `parse_report()`：五段缺任一段即整份拋出 `ReportParseError`。
- [x] 3.4 訊號未就緒或偵測失敗時不生成，保留可於訊號補齊後重新生成的狀態
      `app/main.py` 的 `_handle_coach_report_click()` 於呼叫生成前檢查
      `session.signal_status == SignalStatus.OK`，未就緒時記
      `failure_reason="signals_not_ready"` 並直接返回。
- [x] 3.5 測試：五段結構齊備；內容可回溯至訊號；解析失敗不落庫半成品
      `tests/test_coach_report.py` 單元測試段（9 項）＋整合測試驗證訊號
      的 reason 真的被送進 coaching 提示。

## 4. 觸發與送達

- [x] 4.1 `_handle_postback()` 依 payload 辨識「查看回饋」並觸發對應 session 的報告生成
      Button Template 的按鈕點擊觸發 `messaging_postbacks`，因此邏輯就放在
      `_handle_postback()`（與本項原本的寫法一致）。命中時
      `background_tasks.add_task(_handle_coach_report_click, ...)`。
      **同時修掉一個真機測出來的 bug**：postback 原本不落庫任何訊息，
      點擊建立的新 session 因此被 4.4「未互動使用者阻擋」判為陌生人，
      報告送不出去（`no_inbound_interaction`）。改為補記一筆入站訊息
      （內容用按鈕文字），維持既有不變式，不動 MessageSender 的共用檢查。
- [x] 4.2 已生成的報告重複點擊時直接送出既有內容，不重新呼叫模型
      `_handle_coach_report_click()` 先查 `report.generated_at is not None`，
      是則跳過生成，直接呼叫 `_deliver_coach_report()`。
- [x] 4.3 報告經既有發訊管線送出（沿用訊息切分與順序保證），成功後記錄送達時刻
      `_deliver_coach_report()` 呼叫既有的 `MessageSender.send_reply()`。
- [x] 4.4 送達時視窗逾期則保留為已生成未送達並記錄原因
      不另寫檢查，沿用 `send_reply()` 的既有前置檢查；`outcome.blocked_reason`
      寫進 log，`coach_reports.delivered_at` 維持 NULL。
- [x] 4.5 測試：重複點擊不重複生成；送達失敗不清除已生成內容
      `tests/test_coach_report.py`：重複點擊 coaching 只呼叫一次；
      視窗逾期送達失敗後 `generated_at` 仍保留、`delivered_at` 維持 NULL。

## 5. 補救路徑

- [x] 5.1 使用者再次點擊查看回饋時，補送該場次已生成未送達的報告
      **2026-09-24 修訂**：原實作是「任何一則新訊息都觸發補送」，真機驗證
      後使用者反饋此舉太突兀（報告與當下對話無關，使用者未必記得先前
      點過邀請）——改為只由使用者**明確再次點擊**按鈕觸發，移除
      `_handle_message()` 裡逐則訊息掃描的邏輯，詳見 design.md 決策 8。
      `_handle_coach_report_click()` 的 `already_generated` 分支本身即是
      補送路徑：已生成就跳過模型呼叫，直接呼叫 `_deliver_coach_report()`。
- [x] 5.2 提供重新生成失敗報告的途徑，與既有的訊號重跑腳本搭配
      新腳本 `scripts/rerun_coach_reports.py`，與 `rerun_signals.py` 同構：
      撈出 `generated_at IS NULL 且 failure_reason 有值` 的場次重試，
      訊號本身未就緒時提示先跑 `rerun_signals.py`。生成成功後嘗試立即送達，
      送不出去就留給使用者下次互動時的 5.1 補送邏輯接手。
- [x] 5.3 測試：補送不重新呼叫模型；補送後正確記錄送達時刻
      `tests/test_coach_report.py` 兩項：不相關的新訊息**不會**觸發自動
      補送（`test_傳送不相關訊息不會自動補送先前未送達的報告`）；再次
      點擊按鈕才會補送且 `delivered_at` 被設定、coaching 未被重新呼叫
      （`test_再次點擊按鈕補送先前未送達的報告`）。

## 6. 端對端驗證

- [x] 6.1 真機實測完整流程：連續對話 → 靜默結束 → 收到邀請 → 點擊按鈕 → 收到報告
- [x] 6.2 人工閱讀至少三份真實報告，確認建議中肯、引用原句正確、無空泛內容
      2026-09-24～09-29 由使用者人工閱讀確認完成。過程中發現並修正一項
      歸因錯誤：session 34 的報告把使用者的敷衍回應（「嗯嗯」）歸因到
      後面才問的晚餐問題，而非緊接在前的上班提問——根因在訊號偵測階段
      （非報告生成），已修正 `app/dialog/signals.py` 的 `format_transcript()`
      合併角色連續泡泡，避免同一次回覆被拆成多行而混淆前後關係。
- [x] 6.3 驗證視窗逾期情境：報告保留未送達，使用者再次點擊按鈕後補送成功
      2026-09-24（session 28）：先驗證「擋下＋保留未送達」（視窗過期不呼叫
      API，`delivered_at` 維持 NULL）；決策 8 改用再次點擊觸發補送後，
      真機重新驗證後半段——清空 `delivered_at` 模擬未送達，使用者真機
      再次點擊同一則邀請按鈕，`delivered_at` 正確寫入，coaching 呼叫次數
      維持不變（2 → 2，未重新生成）。
- [x] 6.4 驗證訊號偵測失敗的 session 不會產出報告
      2026-09-24：直接呼叫 `_handle_coach_report_click`（不經真機，因為測的
      是內部判斷邏輯，postback 機制本身已在 6.1 驗證過），將 session 29 的
      `signal_status` 強制設為 `failed` 後觸發，確認 `generated_at` 維持
      `None`、`content` 為空、`failure_reason='signals_not_ready'`，
      coaching 路徑完全未被呼叫。
- [x] 6.5 記錄 coaching 路徑的實際額度消耗，評估最終採用的模型
      **結論（2026-09-29）：維持 `gemini-3.5-flash-lite`，不換模型。**
      這同時回答 design.md Open Questions 的「報告路徑最終採用的模型」。

      實測 4 次真實呼叫（模型皆為 `gemini-3.5-flash-lite`）：

      | # | 耗時 | prompt | completion | 合計 |
      |---|---|---|---|---|
      | 1 | 2942ms | 453 | 300 | 753 |
      | 2 | 1965ms | 635 | 268 | 903 |
      | 3 | 1757ms | 439 | 193 | 632 |
      | 4 | 2840ms | 636 | 300 | 936 |

      平均約 2.4 秒／806 tokens，區間穩定（632–936）。

      判斷依據：
      - **額度**：對照 Gemini 免費層 flash-lite 額度（RPD 500／TPM 250K），
        單次用量與呼叫頻率都遠低於上限，啟用 coaching 路徑不構成額度風險
        （design.md 風險段「首次啟用 coaching 路徑，額度消耗增加」據此解除）
      - **品質**：6.2 已由使用者人工閱讀三份真實報告確認內容中肯、引用正確，
        代表現用模型的產出品質已達可用水準，沒有換更強模型的必要
      - design.md 決策 4 保留的「可改用推理能力較強的模型」選項不必動用；
        該決策同時載明此事可純由設定調整，日後若報告品質不足仍可隨時更換
- [x] 6.6 全套 pytest 綠燈
      **2026-09-29 最終：272 passed**（本 change 累計新增 24 項——
      test_coach_report.py 18 項、test_sender.py 3 項、test_signals.py 3 項）。
      其中 test_signals.py 的 3 項是 6.2 發現歸因錯誤後補的泡泡合併測試。
      過程中順帶修正 `.env` 的 `COACHING_PROVIDER` 誤填為模型名稱
      （應為 `gemini`）——這個既有的錯字讓 `app.main` 在任何情況下都無法啟動
      （`get_settings()` 於模組層級即時驗證三條路徑），與本 change 的程式
      改動無關，屬於修復既有環境設定。
