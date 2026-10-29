---
name: 晴晴的活頁本
description: 終端使用者的設定頁與教練回饋頁共用的白色活頁紙世界：深藍印刷字、藍色原子筆、淺藍螢光筆
colors:
  paper: "#ffffff"
  paper-hover: "#f2f7ff"
  rule: "#dce8fb"
  rule-strong: "#a9c4f2"
  print: "#13254a"
  print-muted: "#56688a"
  ink: "#1a5fe0"
  ink-pressed: "#1550c4"
  highlight: "#dceaff"
  redpen: "#d33434"
typography:
  display:
    fontFamily: "Huninn, PingFang TC, Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "1.875rem"
    fontWeight: 400
    lineHeight: 1.15
  headline:
    fontFamily: "Huninn, PingFang TC, Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "1.625rem"
    fontWeight: 400
    lineHeight: 1.2
  title:
    fontFamily: "Huninn, PingFang TC, Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "1.375rem"
    fontWeight: 400
    lineHeight: 1.2
  body:
    fontFamily: "system-ui, -apple-system, PingFang TC, Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
  body-written:
    fontFamily: "system-ui, -apple-system, PingFang TC, Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 400
    lineHeight: 1.6
  body-letter:
    fontFamily: "system-ui, -apple-system, PingFang TC, Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 400
    lineHeight: 1.85
  label:
    fontFamily: "system-ui, -apple-system, PingFang TC, Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 700
    lineHeight: 1.6
    letterSpacing: "0.02em"
  meta:
    fontFamily: "system-ui, -apple-system, PingFang TC, Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.6
    fontFeature: "tnum"
rounded:
  check: "4px"
  sheet: "6px"
  control: "8px"
  button: "10px"
  pill: "999px"
spacing:
  margin-x: "18px"
  content-left: "34px"
  content-right: "18px"
  margin-x-desktop: "64px"
  content-left-desktop: "92px"
  content-right-desktop: "48px"
  field-top: "18px"
  field-bottom: "12px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.button}"
    padding: "0 20px"
    height: "48px"
  button-primary-hover:
    backgroundColor: "{colors.ink-pressed}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.print}"
    rounded: "{rounded.button}"
    padding: "0 20px"
    height: "48px"
  button-ghost-hover:
    backgroundColor: "{colors.paper-hover}"
  button-small:
    rounded: "{rounded.button}"
    padding: "0 16px"
    height: "40px"
  button-cover:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.button}"
    padding: "0 18px"
    height: "40px"
  button-cover-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.button}"
    padding: "0 18px"
    height: "40px"
  button-danger:
    backgroundColor: "{colors.redpen}"
    textColor: "{colors.paper}"
    rounded: "{rounded.button}"
    padding: "0 16px"
    height: "40px"
  text-button:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    padding: "4px 0"
  line-input:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.body-written}"
    padding: "8px 0 6px"
  plain-select:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    height: "44px"
  tab:
    backgroundColor: "transparent"
    textColor: "{colors.print-muted}"
    padding: "12px 10px 10px"
  tab-active:
    textColor: "{colors.print}"
  type-tag:
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  letter:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.print}"
    rounded: "{rounded.sheet}"
---

# Design System: 晴晴的活頁本

## Overview

**Creative North Star: "白色活頁本"**

這套系統是一本白色活頁本：淺藍頁邊線、淺藍橫線，題目、標題和說明是印好的深藍字，使用者寫的、說過的話是藍色原子筆字。設定頁是替角色填的同學錄，一頁一個區段；教練回饋頁是同一本活頁裡疊起來的信紙，一段對話一張。兩頁的顏色、字體、按鈕和封面帶都一樣，看起來就是同一個產品。

這是安靜、清爽、亮色的紙：一律白底，不做深色模式；對比清楚但不壓迫。手機上用整個螢幕寬度，只在左側留一條頁邊線；寬度 720px 以上時，紙張最寬 680px，放在白色桌面上，有圓角和柔和的影子。版面密度是「一題一條橫線」：每個欄位、每行內文都坐在一條淺藍線上。

顏色有固定意思，不拿來裝飾：藍色原子筆＝使用者寫的或說過的話；淺藍螢光筆＝改過的值（設定頁）或建議的新說法（回饋頁）；紅筆＝錯誤，只出現在設定頁和連結錯誤的提示，回饋信紙上不出現。頁面文字用中立的設定頁口吻，畫面上只出現角色名字，沒有產品名稱或 logo。

**Key Characteristics:**
- 永遠白紙白底，`color-scheme: light`，不跟著系統切換深色
- 淺藍頁邊線（距左 18px，桌面 64px）與淺藍橫線貫穿兩個頁面
- 印刷字（深藍）與原子筆字（藍）的分工就是資訊架構
- 粉圓體只用在標題和信紙抬頭，內文一律系統黑體
- 動態都是「書寫」：螢光筆從左畫到右、筆跡一行一行寫過去、字從左到右寫上去；減少動態時全部取消

## Colors

白紙加一支藍筆：一個藍色當主角，深藍當印刷字，淺藍當紙上的線和螢光筆，紅色只在出錯時出現。

### Primary
- **藍色原子筆**（`ink`）：使用者的值（輸入框、下拉選單選到的值、多項清單）、回饋信紙裡「」內使用者說過的原句、信紙抬頭的日期、主要按鈕底色、勾選框打勾、焦點外框、游標顏色。
- **按下的原子筆**（`ink-pressed`）：主要按鈕滑過時的底色。

### Secondary
- **淺藍螢光筆**（`highlight`）：畫在字的下半部（約 56%–94% 的高度），設定頁表示「這個值改過」，回饋頁表示「這是可以試的新說法」；頁籤上改過的那一頁亮一個螢光點；選取文字的底色。

### Tertiary
- **紅筆**（`redpen`）：欄位驗證錯誤（欄位名稱變紅並加波浪底線、下方紅字說明）、頁籤上的錯誤點、儲存失敗或連結到期的橫幅（紅色 10% 疊白紙當底）、連結錯誤頁的圖示、「恢復預設」的確認按鈕。

### Neutral
- **白紙**（`paper`）：頁面、桌面、封面帶、信紙，全部是白色；也是按鈕上的字色。
- **紙面滑過**（`paper-hover`）：外框按鈕和圖示按鈕滑過時的淺藍白。
- **淺藍橫線**（`rule`）：欄位分隔線、信紙內文的橫線、頁邊線、頁籤列和翻頁列的分隔線、清單的虛線。
- **深一點的橫線**（`rule-strong`）：輸入框底線、外框按鈕的 1.5px 內框、信紙抬頭下的雙線、自訂表單的虛線框、捲軸。
- **深藍印刷字**（`print`）：標題、欄位名稱、印好不可改的值、信紙內文、段落小標。
- **淡印刷字**（`print-muted`）：說明文字、頁碼、預設值提示、placeholder、未選中的頁籤、停用規則的刪除線文字、信紙的等候說明。

### Named Rules
**The 藍筆是你寫的 Rule.** 藍色原子筆字只代表使用者自己寫的值或說過的話。系統文字、說明、標題一律用深藍印刷字；不要因為「想強調」就把系統文字改成藍色。（例外只有互動元件本身：主要按鈕、文字按鈕、焦點外框、信紙抬頭日期。）

**The 螢光筆是新的 Rule.** 淺藍螢光筆只標「跟原本不一樣」的東西：設定頁裡改過的值，回饋頁裡建議的新說法。不拿來當一般的強調或區塊底色。

**The 紅筆不批改練習 Rule.** 紅筆用在錯誤與危險操作，絕對不用在教練回饋上。回饋信紙裡沒有紅字、沒有分數、等第、勾叉或對錯記號；「可以再調整的地方」和其他段落用一樣的深藍印刷字和藍筆原句。

**The 永遠白紙 Rule.** 背景一律白色（`paper`），`theme-color` 是 `#FFFFFF`，`color-scheme` 固定為 light。不做深色模式，不換灰底、米白或有色背景（2026-10-15 經三輪配色確認）。

## Typography

**Display Font:** Huninn 粉圓體（從 Google Fonts 載入，退回 PingFang TC、Noto Sans TC、Microsoft JhengHei）
**Body Font:** system-ui 系統黑體（-apple-system、PingFang TC、Noto Sans TC、Microsoft JhengHei）

**Character:** 粉圓體圓潤、像手寫的大字，只用在標題和信紙抬頭，帶出筆記本的親切感；內文用系統黑體，在 IG 內建瀏覽器裡不用等字型下載也讀得清楚。粉圓體只有 400 一種粗細，不做粗體。

### Hierarchy
- **Display**（粉圓體 400，1.875rem，桌面 2.25rem，行高 1.15）：設定頁的頁名（「身分」等）、已儲存頁與連結錯誤頁的標題；`text-wrap: balance`。
- **Headline**（粉圓體 400，1.625rem，桌面 1.875rem，行高 1.2）：信紙抬頭的日期，藍筆色；時段字縮到 0.78em、同一行、等寬數字。
- **Title**（粉圓體 400，1.375rem，桌面 1.625rem，行高 1.2）：封面帶上的「晴晴的設定」「晴晴的聊天回饋」，單行，過長時以省略號截斷。較小的粉圓體標題：讀完信紙的結語（1.25rem）、自訂表單的標題（1.125rem）。
- **Body**（系統黑體 400，1rem，行高 1.6）：全站內文基準。使用者寫的值和印好的值放大到 1.0625rem（印好的值用 500 粗細）。
- **Body Letter**（系統黑體 400，1.0625rem，桌面 1.125rem，行高 1.85）：信紙內文，一行最多 34em，行高和橫線間距一致；`text-wrap: pretty`。
- **Label**（系統黑體 700，0.8125rem，字距 0.02em）：欄位名稱。信紙的段落小標用同樣的處理，放大到 0.9375rem。
- **Meta**（系統黑體 400，0.8125rem，等寬數字）：剩餘時間、頁碼、預設值提示、規則數量。

### Named Rules
**The 粉圓體只寫標題 Rule.** 粉圓體只用在頁名、封面標題、信紙抬頭和少數結語標題；欄位、按鈕、內文一律系統黑體。

**The 16px 底線 Rule.** 任何會叫出鍵盤或原生選單的元件，字級至少 16px（1rem 以上），避免 iOS 自動放大畫面。

## Layout

一欄、窄紙、由上往下。紙張最寬 680px、置中。三個位置變數控制所有水平對齊：頁邊線位置（`margin-x`）、內容左緣（`content-left`）、內容右緣（`content-right`）；手機是 18／34／18px，寬度 720px 以上是 64／92／48px。封面帶、頁籤、頁面、翻頁列、信紙內文全部對齊同一個內容左緣。

- **封面帶**：固定在頂端，高 64px（桌面 76px），加上 safe-area；左邊是角色名字的標題與一行小字，右邊一個按鈕。
- **設定頁**：封面帶下面是固定的頁籤列（可橫向捲動，右側漸隱），中間是頁面內容（上 28px、桌面 36px、下 40px），底部是固定的翻頁列（上一頁／下一頁，最後一頁換成儲存）。
- **欄位節奏**：每個欄位上 18px、下 12px，底下一條淺藍線；欄位名稱和值之間 6px。可點的列最少高 44px，按鈕最少高 48px（小按鈕 40px）。
- **回饋頁**：信紙由上往下疊成一欄。手機上信紙左右各留 10px（露出桌面），信紙之間 33px（桌面 41px）；桌面上信紙與紙張同寬。最後是結語和回到聊天的按鈕。
- **說明文字寬度**：頁面說明 34em、提示頁內文 28em、信紙內文 34em、信紙說明 30em。

## Elevation & Depth

紙張用很淡的藍灰影子表示「放在桌上」，其他元件都是平的。影子顏色統一來自同一組深藍 RGB（20 50 120），只調透明度。手機版的設定頁沒有影子（紙就是整個螢幕）；回饋頁的信紙在手機上也有影子，因為它們是一張一張疊在桌上的。

### Shadow Vocabulary
- **桌上的活頁紙**（`0 1px 2px rgb(20 50 120 / 0.08), 0 18px 44px -10px rgb(20 50 120 / 0.22)`）：720px 以上設定頁與提示頁的整張紙。
- **疊起來的信紙**（外框 1px 淺藍線，加上往下露出 6px 與 11px 的兩道紙邊，再加 `0 1px 2px rgb(20 50 120 / 0.06), 0 16px 26px -16px rgb(20 50 120 / 0.3)`）：每張回饋信紙。紙邊是信紙世界本身的材質，不是裝飾性的硬投影。
- **翻頁列**（`0 -8px 18px -12px rgb(20 50 120 / 0.22)`）：底部固定列往上的淡影，表示內容從它底下捲過去。
- **封面按鈕**（`0 1px 2px rgb(20 50 120 / 0.12)`）：封面帶上實心按鈕的一點點厚度。

### Named Rules
**The 紙放在桌上 Rule.** 只有「一整張紙」會有影子；紙上的欄位、按鈕、清單都是平的，用橫線和留白分組，不用卡片或色塊。

## Shapes

紙是微圓角（6px），控制元件稍圓一點（按鈕 10px、圖示按鈕和下拉框 8px、勾選框 4px），類型標籤是膠囊形（999px）。線條是主要的造形語言：實線分隔欄位、虛線分隔多項清單與「恢復預設」區、1.5px 虛線框住自訂表單（像紙上留空的欄）、3px 雙線放在信紙抬頭下面。停用的規則用刪除線，像被劃掉。

## Components

### Buttons
簡單、實在、有手感：按下去往下沉 1px。
- **Shape:** 微圓角（10px），最少高 48px，左右 20px；粗體 1rem，可帶 Lucide 線條圖示（17–18px，線寬 2）。
- **Primary:** 藍筆實心底、白字，用在推進的動作（下一頁、儲存、回到聊天、重新整理）。
- **Hover / Focus:** 滑過時底色變成 `ink-pressed`；鍵盤焦點是 2px 藍筆外框，間距 3px；停用時透明度 0.45。轉場 150ms ease-out。
- **Ghost:** 透明底、深藍字、1.5px 內框（`rule-strong`），滑過時填紙面滑過色。用在上一頁、再試一次、新增規則。
- **Small:** 高 40px、左右 16px、0.9375rem。
- **封面按鈕:** 高 40px。設定頁是實心藍「儲存」，右上角有白點表示有未儲存的修改；回饋頁用安靜版（透明底、藍字、淺藍內框）的「回到聊天」，讓實心按鈕留在頁尾。
- **Danger:** 紅筆實心底，只用在「確認恢復預設」。
- **Text button:** 藍筆色、0.875rem、600、底線（偏移 3px），用在「改回預設」「取消」這類次要動作。
- **Icon button:** 40×40px、8px 圓角、淡印刷色，滑過時填紙面滑過色並變深藍。

### Chips
- **類型標籤：** 膠囊形、1px `currentColor` 外框、0.75rem 粗體、無底色；文字顏色依規則類型而定（亮點＝藍筆、殺手＝淡印刷字）。

### Cards / Containers
這套系統沒有卡片。容器只有兩種「紙」：
- **活頁紙**（設定頁、提示頁）：白底，720px 以上 6px 圓角加桌上影子；左側一條 1px 淺藍頁邊線貫穿全高。
- **自訂表單**：1.5px 虛線框（`rule-strong`）、10px 圓角、內距 16px，像紙上一塊留白讓人填。

### Inputs / Fields
寫在橫線上，不是方框。
- **單行輸入：** 無框無底色，只有底線；值是藍筆字 1.0625rem。直接放在欄位裡時不畫第二條線，聚焦時底線變藍筆並加粗 1px。
- **下拉選單：** 看起來是寫在線上的藍筆字加一個向下箭頭，實際是蓋在上面的透明原生 select；聚焦時 2px 藍筆外框。
- **長段落：** textarea 本身畫著 1.75rem 間距的橫線（隨內容捲動），字寫在線上。
- **勾選框：** 22px 印好的小方格（1.5px 深藍框、4px 圓角），勾選後填藍筆色，白色勾以 160ms 彈出。
- **改過：** 值的下半部畫上淺藍螢光筆，460ms 從左畫到右（長段落改成整塊 24% 的淡螢光底）。
- **印好的值：** 名字、IG 名稱、系統底線用深藍 500 字加鎖頭圖示，不能改。
- **Error:** 欄位名稱變紅並加紅色波浪底線，下方紅字（0.875rem、600）加筆形圖示說明原因。
- **Plain select（自訂表單內）：** 1px `rule-strong` 外框、8px 圓角、高 44px、藍筆字。

### Navigation
- **頁籤：** 固定在封面帶下方，可橫向捲動，右側 12% 漸隱。未選中是淡印刷字 0.9375rem；目前頁是深藍粗體加 2px 藍筆底線。改過的頁有 7px 螢光點，有錯誤的頁是紅點。
- **翻頁列：** 底部固定，白底、上方淺藍線與淡影；左邊外框按鈕「上一頁」，右邊實心按鈕（最寬 260px）。

### 回饋信紙（Signature Component）
一段對話一張信紙，由上往下疊。
- **抬頭：** 粉圓體藍筆日期，時段縮小接在同一行（手機上日期和時段各自不拆行，會整齊分成兩行），下面一道 3px 雙線。
- **段落：** 段落小標用深藍印刷字粗體；內文深藍，坐在淺藍橫線上（行高 1.85）。「」內是使用者說過的原句，用藍筆色 500；「換個說法試試」段落裡的「」是建議的新說法，藍筆字加淺藍螢光筆。所有段落同一個樣式，沒有分數、顏色分級或對錯記號。
- **整理中：** 一句說明加五條空白橫線（奇數行短一點），藍筆筆跡（1.6px 圓頭的手寫波浪 SVG）一行一行寫過去，一輪 6 秒，每行延遲 0.9 秒。
- **排隊中：** 淡印刷字說明加空白橫線，沒有筆跡。
- **寫好時：** 每個段落用 clip-path 從左到右寫上去（900ms，段落間隔 140ms）。
- **失敗：** 一句說明加一顆小外框按鈕「再試一次」；不用紅色。
- **減少動態：** 不畫筆跡，段落直接出現。

### 提示頁
已儲存、連結過期、連結打不開、伺服器連不上：活頁紙上一個 28px 線條圖示（成功用藍筆色；設定頁的連結錯誤用紅筆色，回饋頁的連結錯誤用藍筆色）、粉圓體大標題、1.0625rem 行高 1.7 的說明，以及回到聊天或重新整理的按鈕。

## Do's and Don'ts

### Do:
- **Do** 讓每個頁面都是白紙：背景 `#ffffff`、`color-scheme: light`、`theme-color #FFFFFF`，系統深色模式時也不變。
- **Do** 使用者寫的值、使用者說過的原句用藍色原子筆（`ink`）；題目、標題、說明、印好的值用深藍印刷字（`print`）。
- **Do** 改過的值（設定頁）和建議的新說法（回饋頁）畫淺藍螢光筆在字的下半部，`box-decoration-break: clone` 讓換行時每行都畫到。
- **Do** 每個欄位、每行內文坐在一條淺藍橫線上；水平位置對齊頁邊線與內容左緣（手機 18／34px，桌面 64／92px）。
- **Do** 標題用粉圓體 400，其他都用系統黑體；會叫出鍵盤的元件字級至少 16px。
- **Do** 頁面文字用中立的設定頁口吻；封面標題寫成「{角色名}的設定」「{角色名}的聊天回饋」，畫面上只出現角色名字。
- **Do** 動態做成書寫的動作（螢光筆畫過、筆跡寫過、字寫上去），並在 `prefers-reduced-motion: reduce` 時全部關掉。
- **Do** 圖示用 Lucide 線條圖示（線寬 2，提示頁大圖示 2.25），裝飾性的圖示加 `aria-hidden`。

### Don't:
- **Don't** 做深色模式、灰底分組清單或有色背景。
- **Don't** 在教練回饋上用紅筆，或顯示分數、等第、星等、勾叉、對錯記號；回饋不能像成績單或考卷。
- **Don't** 把系統文字寫成藍筆色，讓「藍筆＝使用者寫的」失去意思。
- **Don't** 把螢光筆當一般強調或區塊底色。
- **Don't** 放產品名稱、logo 或客戶品牌；不讓角色本人在頁面文案裡說話。
- **Don't** 把欄位或回饋段落包成卡片牆；分組用橫線和留白，影子只給整張紙。
- **Don't** 把欄位做成方框輸入；值是寫在線上的字。唯一的外框控制是自訂表單裡的小下拉選單。
