# nchu-course-watch

監控中興大學通識課餘額，有名額時推播 LINE 與 Telegram，並可從 Telegram 遙控。
macOS / Windows 都可以跑。

```bash
./nchu login               # 登入並存下 cookie
./nchu check               # 看一次監控清單的餘額
./nchu watch               # 常駐監控，有名額時推播；可用手機遙控
./nchu watch --autoadd     # 同上，而且一有名額就自動幫你加選
```

---

## 為什麼不用每次都重新登入

登入走的是中興的 Apereo CAS（`ccidp.nchu.edu.tw`），表單上掛了一個 Cloudflare
Turnstile。但 **Turnstile 只存在於登入頁**，課程查詢頁（`cportal.nchu.edu.tw`）
完全沒有，而且兩個網域都是學校自架 nginx，沒有 Cloudflare 全站防護。

利用 CAS 的 SSO 機制，查詢分成三層，瀏覽器只在最外層出現：

| 層級 | 條件 | 做法 | 實測 |
|---|---|---|---|
| **L1** | cportal 的 `SESSION` cookie 有效 | 直接 POST 查詢 | **0.7s** |
| **L2** | `SESSION` 過期、CAS 的 `TGC` 還在 | 用 TGC 換 service ticket 再查詢（**不需密碼、不需 Turnstile**） | **0.9s** |
| **L3** | `TGC` 也過期 | 開 SeleniumBase UC 瀏覽器走密碼登入 | 14s |

cookie 存在 `data/cookies.json`（macOS 上權限 600），跨執行保留。每分鐘輪詢絕大多數落在 L1。

---

## 安裝

**macOS / Linux**

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env      # 填入學號、密碼、LINE token（Telegram 選填）
```

**Windows**

```
python -m venv .venv
.venv\Scripts\pip install -e .
copy .env.example .env
```

`./nchu`（macOS）與 `nchu.cmd`（Windows）都會自動使用 `.venv`，所以裝完之後不用
activate 也不用管路徑。`pip install -e .` 之後也可以直接用 `.venv/bin/nchu`
或 `.venv\Scripts\nchu.exe`。

## 設定

| 檔案 | 放什麼 |
|---|---|
| `.env` | 學號、密碼、LINE channel access token、Telegram token 與 chat id |
| `settings.toml` | 監控哪些課、輪詢間隔、離峰時段、重試策略 |

日常只會動 `settings.toml`，不用碰任何 Python 檔。

帳號密碼一律用 `NCHU_` 前綴（`NCHU_USERNAME` / `NCHU_PASSWORD`）。Windows 內建的
`USERNAME` 環境變數會永遠存在，所以程式刻意不接受無前綴的版本。

`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 是選填，不填就只有 LINE 通知、也不能
從手機下指令，其餘功能不受影響。

> **選課號碼每學期重新編號。** 同一個 `0106`，上學期是「太極拳基礎」，
> 這學期變成「視覺藝術欣賞」。換學期請上選課系統重新查一次，再把課號填回
> `settings.toml`。`nchu check` 也會提示哪些課號這學期查不到。

## 指令

| 指令 | 用途 |
|---|---|
| `nchu check` | 查一次監控清單的餘額並列表 |
| `nchu check -a` | 只列還有名額的 |
| `nchu check -n` | 狀態有變就推播 LINE / Telegram（給排程用） |
| `nchu check -p` | 把整份監控清單推播到 LINE / Telegram（不管狀態有沒有變） |
| `nchu check -v` | 顯示 session 走哪一層 |
| `nchu watch` | 常駐監控，狀態改變時推播，可用 Telegram 遙控 |
| `nchu watch --autoadd` | 同上，並且**一有名額就自動幫你加選** |
| `nchu watch --dry-run` | 自動加選演練：走到最後一步就停，不會真的送出 |
| `nchu enroll <課號>` | 手動加選一門課（`--dry-run` 只演練） |
| `nchu telegram` | 找出自己的 chat id（`--test` 送一則測試訊息） |
| `nchu login` | 強制重新登入（`--fresh` 會先清掉舊 cookie） |
| `nchu logout` | 刪掉 cookie，下次查詢重新登入 |

macOS 上前面加 `./`（`./nchu check`）。

### 讓 watch 一直跑

沒有內建的背景服務，`watch` 就是一個前景程式，開一個終端機視窗讓它跑著即可，
Ctrl-C 會等本輪結束後乾淨退出。想開機自動啟動的話：

**Windows（工作排程器）**

```
schtasks /create /sc onlogon /tn nchu-watch /tr "C:\path\to\webscrape-nchu\nchu.cmd watch"
```

**macOS（launchd）** — 自己寫一份 LaunchAgent plist 指向 `<專案路徑>/nchu watch`，
搭配 `RunAtLoad` + `KeepAlive`。

要留 log 就自己導向檔案：`nchu watch > watch.log 2>&1`
（輸出不是終端機時會自動關掉顏色，log 不會有 ANSI 雜訊）。

## 用 Telegram 遙控

`watch` 跑著的時候，可以從手機控制它，不必回到電腦前。

### 設定（一次就好）

1. 在 Telegram 找 **@BotFather**，`/newbot` 建一個 bot，把它給的 token 填進 `.env`
   的 `TELEGRAM_BOT_TOKEN`
2. 打開自己的 bot，對它說一句話（例如 `/start`）
3. 執行 `./nchu telegram`，把印出來的 `TELEGRAM_CHAT_ID=...` 貼進 `.env`
4. `./nchu telegram --test` 確認送得出訊息

沒有 webhook、不用架伺服器、不用 ngrok 或 cloudflared，也不必開任何連接埠——
收指令走 Telegram 的 `getUpdates` 長輪詢，是**我們主動去拉**，所以「等下一輪查詢」
與「監聽指令」是同一條執行緒的同一件事。

### 指令

| 指令 | 按鈕 | 作用 |
|---|---|---|
| `/now` | 🔄 | 立刻查一次並回報，暫停中一樣能用 |
| `/add <課號>` | 加選一門課，例如 `/add 0117`。會先問一次再送出 |
| `/go` | ▶️ | 恢復定時查詢，並立刻查一次（不必等剩下的間隔） |
| `/stop` | ⏸ | 暫停定時查詢 |
| `/status` | — | 重發狀態面板並重新置頂（面板被誤刪時用） |
| `/quit` | — | **關閉整個程式**（限群組管理員，會先問一次） |

指令會註冊到輸入框旁的選單，不用背；也可以直接按面板上的按鈕。

> `/stop` 是**暫停輪詢，不是結束程式**：程式留著才聽得到 `/go`，而且 `caffeinate`
> 的防睡眠也還綁在行程上，筆電依然不能睡。真的要收工用 `/quit`（或在終端機按
> Ctrl-C），兩者都會把面板標示成「已停止」。

### 權限

查詢與暫停（`/now`、`/go`、`/stop`、`/status`）群組裡**每個人都能用**——本來就是
要一起看餘額的。只有 `/quit` 限**群組管理員**，因為關掉之後沒有任何行程在聽指令，
要重開只能回到電腦前。

管理員名單直接跟 Telegram 要（`getChatAdministrators`），所以不必另外維護設定：
你在群組資訊裡把誰設成管理員，誰就能關。名單查不到時（斷網、bot 被踢出群）一律
拒絕，不會在權限不明的情況下放行。私聊模式沒有管理員這回事，一律放行。

### 狀態面板

啟動時會發一則訊息並**置頂**，之後每輪就地改寫同一則，不會洗版：

```
🟢 監控中 · 6 門 · 2 門有名額 · 14:32

✅ 0532 統計學導論 — 3 位
⬜ 0229 台灣文學 — 額滿
...

⚠️ 這學期查無課號 0111（課號每學期會重編）
下次查詢 14:33

[ 🔄 立即查詢 ]  [ ⏸ 暫停監控 ]
```

置頂橫幅在手機上只看得到**第一行**，點開才會展開全文，所以第一行固定是完整摘要。

程式每次啟動都會重發一則新的面板並取消舊的置頂，不沿用上次的 message id；停機
期間累積的指令也會在啟動時丟掉，免得開機瞬間把昨晚傳的 `/stop` 補跑一遍。

## 自動加選

`nchu watch --autoadd` 會在發現名額的那一秒直接把課加下來，走的是純 HTTP，
**一次約 0.7 秒**（開瀏覽器要 15 秒），搶課時差很多。

對應的手動操作是教務資訊系統左側選單的「輸入課號加選」：

```
填課號 →「確定送出」→ 勾選那門課 →「是，確定加選」
```

### 進入選課系統要兩段登入

課程查詢頁只要 CAS 的 session 就能用，但選課功能屬於**教務資訊系統**，
光有 CAS session 會看到「已閒置過久或名稱密碼有誤」。實際要多一次 SSO 轉手：

| 步驟 | 網址 |
|---|---|
| 1. 校園入口登入 | `cportal.nchu.edu.tw/cas_login/`（沿用監控的 L1/L2/L3） |
| 2. 教務系統 SSO | `cas_login/acad?p_subname=enro_direct1_list` → 自動 POST `cof_ssologin` |
| 3. 填課號 | `GET /cofsys/plsql/enro_direct1_list` |
| 4. 確認頁 | `POST /cofsys/plsql/enro_direct2_chk` |
| 5. 真正寫入 | `POST /cofsys/plsql/enro_direct3_dml` |

### 這支程式不會退你的課

因為端點是寫死的，「只加不退」是結構上的保證，不是靠猜頁面上的按鈕：

| 防線 | 做法 |
|---|---|
| **POST 白名單** | 全程只 POST 上表那三個網址，其他一律拒送。退選頁（`enro_del1_list`、`enro_rule1_list?v_type=D`、`enro_stop_list`）不在名單內，程式裡**沒有任何路徑走得到** |
| **網址黑名單** | 網址含 `_del` / `delete` / `drop` / `_stop` / `cancel` / `remove` / `v_type=d` 一律拒送，等於白名單之外再上一道鎖 |
| **送出前驗表單** | 確認頁的 `<form action>` 必須正好是 `enro_direct3_dml`，不是就中止 |
| **勾選碼只從頁面取** | `v_tick` 一定來自「那一列的課號欄**整格等於**目標課號」的那個 checkbox，不自己編，也不送別列的勾選碼 |
| **一次只送一門** | 表單有 10 格，只填第一格，其餘留空 |
| **事後驗證** | 加選後再抓一次選課清單確認，結果不靠關鍵字猜 |

任何一步跟預期不符就中止，而且中止時什麼都還沒送出。

### 用法

```bash
./nchu watch --autoadd          # 有名額就自動加選
./nchu watch --dry-run          # 演練：走到送出前就停
./nchu enroll 0117 --dry-run    # 單獨測一門課，會印出確認頁對到哪一列
./nchu enroll 0117 -y           # 手動加選一門課
```

實際跑起來長這樣：

```
確認頁對到課號 0117：| 0117 | 探索臺中城 | 夜不限系別 | 半 | 2 | 必 | 蘇全正 | 50 | 36 | 0 |
加選 0117 結束：加選成功（0.7s）
```

### 從手機加選：`/add`

`watch` 跑著的時候，在 Telegram 打 `/add 0117` 就能遠端加選，不必回電腦前。

```
你：/add 0117
bot：要加選這門課嗎？

     0117 探索臺中城
     授課教師：蘇全正
     學分：2
     人數：36 / 50（候補 0）
     [✅ 確定加選] [取消]
```

- 打錯課號會加到完全不相干的課（課號每學期重編），所以**先演練一次把課程資訊撈回來
  給你核對**，按下按鈕才真的送出。演練只花 0.5 秒，不影響搶課。
- 不用開 `--autoadd` 也能用——`/add` 是你自己打的、而且按了確認才送出。
- 結果一樣寫進 `data/enroll.json`，所以手動加到的課，自動加選不會再送一次。
- 群組裡限管理員使用（和 `/quit` 同一套權限檢查）；私聊不受限。

### 想確認到底做了什麼

教務系統的**選課足跡**（左側選單，`footprint_stuqry`）會記錄每一次加退選的時間與動作，
比任何程式 log 都可信。懷疑時就去那裡對，例如：

```
2026-09-07 13:35:41  0117 探索臺中城  新增加選。
```

### 行為細節

- **預設關閉**，要嘛加 `--autoadd`，要嘛把 `settings.toml` 的 `[enroll] auto` 設成 `true`。
- 加選目標預設等於監控清單；要只加其中幾門就填 `[enroll] codes`。
- **連續失敗 3 次就不再試那門課**（`max_attempts`），成功則次數歸零。
  想重來把 `data/enroll.json` 刪掉即可。
- 衝堂、已選過這類「再試也一樣」的結果，一次就停。
- 結果會推播 LINE / Telegram，Telegram 面板下方也會留最近三筆。
- 失敗原因直接讀結果頁的「選課結果」欄，例如
  `加選失敗，本學期已選修超過4門通識課程`，不是自己猜的。
- 加選頁面快照存在 `data/enroll-debug/`，結果頁一律留檔（出事才有證據）。

## 通知規則

**狀態變更通知 LINE 與 Telegram 兩邊都會送**；`/now` 之類的指令回覆只回 Telegram，
免得每按一次 LINE 就跟著響（LINE 免費方案每月只有 200 則推播）。

只在**狀態翻轉**時推播，不會每分鐘洗版：

- 餘額 `0 → >0`：`【有名額】`
- 餘額 `>0 → 0`：`【已額滿】`
- 數字變動但仍有名額（例如 `3 → 2`）：不推播

上一輪的狀態存在 `data/state.json`。首次啟動會發一則現況摘要。
連續失敗 3 次會發一次告警，恢復後再發一則。

`/now` 手動查詢一樣會更新 `state.json`，否則恢復自動輪詢時會拿舊狀態比對而漏報。

通知訊息一律純文字、不含 emoji，避免 Windows 在 cp950 編碼下寫 log 時炸掉；
Telegram 的狀態面板不進 log，所以不受這條限制。

## 專案結構

```
nchu                        # CLI 包裝腳本（macOS / Linux）
nchu.cmd                    # CLI 包裝腳本（Windows）
settings.toml               # 使用者設定
.env                        # 機密
data/                       # 執行期產物（cookies / state），已 gitignore
src/nchu_course/
├── cli.py                  # 所有指令
├── config.py               # 讀 settings.toml + .env，定義路徑與站台常數
├── session.py              # 三層 session 續命與 cookie 持久化
├── browser_login.py        # L3 的 SeleniumBase UC 密碼登入
├── scraper.py              # 查詢 payload 組裝與 HTML 解析
├── notifier.py             # 狀態去重通知（LINE + Telegram 雙發）
├── daemon.py               # 常駐輪詢迴圈，等待空檔兼作指令監聽
├── control.py              # Telegram 指令處理與置頂狀態面板
├── enroll.py               # 自動加選（純 HTTP，只 POST 三個寫死的加選端點）
├── line.py                 # LINE Messaging API
├── telegram.py             # Telegram Bot API（getUpdates 長輪詢）
└── ui.py                   # 終端表格（處理全形字寬與 ANSI 色碼）
```

## 注意

- `data/` 已列入 `.gitignore`。`cookies.json` 裡的 `TGC` 等同登入憑證，**不要 commit**。
- cookie 檔的 `chmod 600` 在 Windows 上是 no-op，沒有實質的權限保護。
- L3 需要已安裝 Chrome，而且要有實體桌面 session
  （Turnstile 的點擊走 PyAutoGUI，也會受顯示器縮放比例影響）。L1 / L2 不受影響。
- `data/enroll-debug/` 裡的頁面快照含你的選課資料，跟 `data/` 一樣不要外流。
- Telegram 的 bot 只認 `.env` 裡的那個 chat id，其他人傳指令會被忽略並記錄在 log。
- Ctrl-C 之後最多會等 10 秒才退出（`TELEGRAM_LONG_POLL`，正掛在長輪詢上）。
- GitHub Actions 的 workflow 只是手動觸發的備援：cron 最短 5 分鐘且尖峰常延遲，
  且每次都是全新 runner 帶不了 cookie，等於每次都走 L3。
