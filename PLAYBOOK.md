# 監票 Playbook 📒

把這個專案的所有 know-how 寫成一份可複用手冊：**架構原理、複製到新活動、換別的售票平台、
踩過的所有坑、做不到的極限、部署維運**。給未來的你、給接手的 AI。

快速版看 [`README.md`](README.md)；這裡是完整版。

---

## 1. 架構與原理

### 為什麼用 API 偵測，不用「瀏覽器點日曆」
- 最早版本用 Playwright 開瀏覽器、一天一天點日曆讀票況 → 掃 16 天**一圈要約 64 秒**，
  等於同一場每 64 秒才看一次，**追不上幾秒就被搶光的釋出**。
- 後來逆向出 Accupass 背後的 API：**一個請求就回傳整個活動所有場次的票況**，一圈 **~1 秒**。
  廣（全日期）和快（秒級回訪）同時拿到。

### Accupass 的兩支 API（eflow-queue）
1. `POST https://eflow-queue.accupass.com/api/GetOrRenewToken?EventIdNumber=<EVENT_ID>`
   - body `{}`，header 要帶 `authorization: oauth_token="<登入權杖>"`
   - 回傳一把**查詢用 token**（約 11 分鐘有效）。
2. `GET https://eflow-queue.accupass.com/api/GetEventTickets?eventIdNumber=<EVENT_ID>&token=<token>`
   - 回傳 `eventTicketList`，每場有 `name`(日期時間)、`soldCount`、`ticketCount`、`ticketStatus`。

### 兩個關鍵實作細節（踩過坑換來的）
- **authorization 標頭怎麼來**：不去翻登入明文。啟動時開一次票券頁，用 `page.on("request")`
  攔下「網站自己發給 eflow-queue 的那個 authorization 標頭」存起來重用。
- **token 一定要 URL 編碼**：token 含 `+ / =`，直接塞進網址會被伺服器解析錯 → 回 **HTTP 400**。
  要 `urllib.parse.quote(token, safe='')`。

### 可報名的判定
```
status 不是 Expired、也不是 SoldOut，且 soldCount < ticketCount
```
（退票時會從 `70/70` 變成 `69/70`，status 從 SoldOut 翻回可賣。）

### 雲端「近 24 小時不停」靠三層
1. **腳本內 loop**：單次執行內部每 3 秒掃一圈，連續跑約 5.5 小時（GitHub 單 job 上限 6h）。
2. **PAT 自我接力**：一棒結束時用 `WATCHER_PAT` 跑 `gh workflow run` 接下一棒
   （GITHUB_TOKEN 不能自我觸發，故用 PAT），棒與棒間只剩 ~1 分鐘。
3. **排程安全網**：`schedule: cron` 定時觸發，萬一接力鏈斷了也能自動復活（concurrency 確保不重複跑）。

### 通知
- Discord Webhook POST。**必帶自訂 `User-Agent`**，否則 Cloudflare 回 403。
- 要真的 @everyone 跳通知＋出聲：payload 加 `allowed_mentions: {"parse": ["everyone"]}`。
- 訊息用 `#` 大標題把「6/X 時段」放最前面，一眼看懂要搶哪場；同一場只 tag 一次防洗頻。

### 兩種模式（環境變數 `NOTIFY_ONLY`）
- `NOTIFY_ONLY=1`：**只通知**，偵測到就 @everyone 叫你手動搶，bot 不碰下單。**持續跑不停**。← 現行
- `NOTIFY_ONLY=0`：**自動卡位**，偵測到就點「+」→「立即報名」衝進訂單頁，**卡到一次寫 `grabbed.flag` 就停**。

---

## 2. 複製到新的 Accupass 活動

1. `auto_grab.py` 最上面改 `EVENT_ID`（新活動網址最後那串數字），需要的話調 `GRAB_DAYS`。
2. 本機 `python export_login.py` → 手動登入 → 產生 `state.json` →
   `gh secret set ACCUPASS_STATE < state.json`。
3. `gh secret set DISCORD_WEBHOOK_URL`（值 = 你的 webhook 網址）。
4. `gh workflow run auto_grab.yml -f dry_run=false` 啟動。

**Secrets 三件套**：`DISCORD_WEBHOOK_URL`、`ACCUPASS_STATE`、`WATCHER_PAT`。

---

## 3. 換別的售票平台（KKTIX / 拓元 tixCraft / ibon / 寬宏…）

⚠️ **這套程式不能直接套**。偵測引擎是針對 Accupass 的 API 逆向出來的；換平台 = API、登入、
選擇器全不同，**偵測引擎要重新逆向**。能照用的是「**方法論 + 雲端/通知骨架**」。

### Step 1：逆向出「票況 API」
1. Chrome 開該活動售票頁，按 **F12 → Network** 分頁，篩選 **Fetch/XHR**。
2. 在頁面上操作（選日期、選票種、按重新整理），看哪個請求**回傳「場次清單 / 庫存數量」的 JSON**。
3. 點那個請求，記下：**URL、method(GET/POST)、query 參數、必要的 headers**
   （`authorization` / `cookie` / 自訂 token）、以及 **response 的欄位**（哪個欄位代表「還有幾張」）。
4. 注意有沒有「**先取 token 再查**」的兩段式（像 Accupass 的 GetOrRenewToken → GetEventTickets）。

### Step 2：處理登入 / 權杖
- 用 Playwright `storageState` 把登入存成 `state.json`（同 `export_login.py`）。
- 若該平台用**自訂 auth 標頭**（不是純 cookie），啟動時用 `page.on("request")` 攔下重用。
- 本專案有一支可改用的攔截 + 探測範本（本機 `probe_flow.py`，已 gitignore），骨架如下：

```python
# 攔下網站自己發的 authorization 標頭，再用同樣 cookie 自己打 API
auth = {"h": None}
page.on("request", lambda r: auth.__setitem__("h", r.headers.get("authorization"))
        if ("api.該平台.com" in r.url and not auth["h"] and r.headers.get("authorization")) else None)
page.goto(售票頁); page.wait_for_timeout(4000)
api = ctx.request                       # 共用 cookie 的 HTTP client
r = api.get(票況API網址, headers={"authorization": auth["h"]})
print(r.json())                          # 看回傳結構，找「庫存」欄位
```

### Step 3：接到同一套骨架
- 把 `auto_grab.py` 的 `api_scan()` 換成「打新平台 API、解析庫存、回傳可報名清單」。
- 通知（Discord @everyone）、雲端（loop + 接力 + 排程）、模式（NOTIFY_ONLY）**整套照用**。

### ⚠️ 付費演唱會的誠實警告
拓元、寬宏這類**付費**售票，通常有：**圖形驗證碼、排隊系統、付款流程、強防機器人**，難度遠高於
這個免費實名活動。而且**自動搶票常違反平台條款**、有黃牛/不公平交易的法律風險。
**強烈建議：付費活動只做「偵測 → 通知你手動搶」，不要做自動下單。**

---

## 4. 已知問題 + 解法總表（這次踩過的所有坑）

| 問題 | 原因 | 解法 |
|---|---|---|
| 純 requests/BS4 永遠誤判「無票」 | Accupass 是動態渲染，原始 HTML 只有翻譯字典 | 改打 API，或用 Playwright 讀渲染後文字 |
| 自動卡位「數量沒加成功」 | 「+」是 `<span class=*-add>` 不是 `<button>` | 用 Playwright 真實點該 span |
| API 一直回 HTTP 400 | token 含 `+ / =` 沒做 URL 編碼 | `quote(token, safe='')` |
| 長跑 log 一片空白、cancel 後全失 | Python 輸出緩衝、被 kill 前沒 flush | 環境變數 `PYTHONUNBUFFERED=1` |
| 雲端換月導航暴衝 | 雲端英文語系顯示 "April" 非 "4月" | `locale="zh-TW"`（API 版已不依賴） |
| 每 5 分鐘排程實際 1~3 小時才跑 | GitHub 對 cron 的降速 | 腳本內 loop + PAT 自我接力 |
| 卡到一張就整段沒通知 | `NOTIFY_ONLY=0` 卡到一次就停 | 只通知模式不會停；或改成持續跑 |
| 收到一堆 workflow 失敗信、互相取消 | 多個 workflow 並存打架 | 單一通知源，停用 watch.yml / 健康日報 |
| Discord 推播 403 | 預設 User-Agent 被 Cloudflare 擋 | 帶自訂 `User-Agent` |
| @everyone 不會跳通知 | 沒給 allowed_mentions | payload 加 `allowed_mentions:{parse:[everyone]}` |
| 想用連結直接帶到某天 | Accupass 票券頁不吃日期參數、永遠停預設日 | 做不到，只能進頁後手動切日期 |

---

## 5. 物理極限（誠實：這些不是 bug，是本質做不到）

- **秒搶型釋出**：有些票幾秒就被搶光，落在兩次偵測之間 → 任何輪詢都追不上。
  （但有些「需審核」票會賴比較久，手動有機會。）
- **半夜釋出**：釋出常在凌晨，人在睡、10 分鐘填單窗口沒人接。
- **個資/付款必須本人**：實名票要填身分證、付費票要付款 → 不可能全自動（個資也不該上雲端）。
- **bot 卡的訂單跨裝置接不到**：自動卡位卡的單綁在 bot 的雲端工作階段，**別的裝置（你手機）接不到、填不了**
  → 所以「只通知 + 你手動」才是最穩解。
- **需審核 ≠ 確定**：很多場次標「需審核」，搶到、填完、送出只是「待審核」，最終要主辦核准。

---

## 6. 部署 / 維運 SOP

### Secrets（GitHub repo → Settings → Secrets → Actions）
| Secret | 用途 | 怎麼設 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | 手機通知管道 | `gh secret set DISCORD_WEBHOOK_URL` |
| `ACCUPASS_STATE` | 登入狀態（state.json 內容） | `gh secret set ACCUPASS_STATE < state.json` |
| `WATCHER_PAT` | 自我接力用（細粒度、only this repo、Actions RW） | `gh secret set WATCHER_PAT` |

> 🔑 **`WATCHER_PAT` 2026-07-09 到期**，到期要重新產生再 `gh secret set`，否則接力會斷。

### 更新登入（cookie 萬一真失效時）
本機 `python export_login.py` 重匯 → `gh secret set ACCUPASS_STATE < state.json`。

### Debug 技巧
- 本機快測：`DRY_RUN=1`、`LOOP_MAX_MINUTES=1` 跑 `auto_grab.py` → 幾圈就結束、看登入/API/票況。
- 雲端讀 log：`gh run view <id> --log`（**執行中讀不到、要等結束或 cancel**；配 `PYTHONUNBUFFERED=1`）。
- 逆向/探測：本機 `probe_*.py`（已 gitignore），攔 API、看回傳結構。

### 啟 / 停
```bash
# 啟（重啟吃新碼）：先停舊棒再 dispatch
gh run cancel <舊棒ID> --repo HaDoLH/accupass-ticket-watcher
gh workflow run auto_grab.yml --repo HaDoLH/accupass-ticket-watcher -f dry_run=false

# 停：到 GitHub Actions 把「Accupass 自動卡位」Disable
```

> ⚠️ 改完 `.py` / `.yml` 一定要 `git push`；**新 dispatch 才會抓最新 commit 的程式碼**。
> 既有的舊棒會繼續跑舊碼，所以要 cancel 舊棒、再 dispatch 新棒。