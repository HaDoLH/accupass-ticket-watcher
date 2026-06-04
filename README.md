# Accupass 票券監看 🎫

每 5 分鐘自動檢查 Accupass 的**指定日期、指定場次**，**一旦釋出名額（從『已售完』變回可報名），
就把通知推到你的手機（Discord）**。跑在 GitHub 雲端伺服器上，**不用開電腦**，週末出門也能監看。

監看目標：SUPER JUNIOR 20TH ANNIVERSARY POP-UP（訂票頁）
`https://www.accupass.com/eflow/ticket/2605080529051188996723`

目前鎖定：**2026/06/13（六）** 的 `19:40-20:20`、`20:30-21:10`、`21:20-22:00` 三個場次。

---

## 為什麼不是用 requests + BeautifulSoup？

Accupass 是 JavaScript 動態渲染的網站。用一般方式抓到的原始 HTML 裡**沒有真實票況**
（「立即報名」「已售完」都只是內嵌的翻譯字典，永遠存在）。所以這支腳本改用
**Playwright 無頭瀏覽器**把頁面 JS 跑完，再讀「畫面上看得到的文字」來判斷，才會準。

---

## 運作方式

```
GitHub Actions（每 5 分鐘）
        │
        ▼
watch_ticket.py ──► Playwright 開無頭瀏覽器、進訂票頁
        │
        ▼
日曆切到指定日期（6/13）→ 讀指定場次是否「已售完」
        │
 有釋出 ─┴─ 都還售完
   │           └─► 印出「都還是已售完」，結束，等下一輪
   ▼
POST 到 Discord Webhook ──► 你的手機跳通知 📱
```

> 為什麼盯訂票頁而不是活動主頁？活動主頁只有一個「立即報名」按鈕，分不出場次；
> 真正的「日期＋場次＋名額狀態」在訂票頁 `/eflow/ticket/<活動ID>` 才看得到。

---

## 部署步驟（一次設定，之後全自動）

### 1. 把這個資料夾推到你自己的 GitHub repo
建議 repo 名稱：`accupass-ticket-watcher`（可設 Private）。

### 2. 在 Discord 建立 Webhook（這就是手機通知的管道）
1. 打開 Discord，挑一個你看得到通知的頻道（自己的私人伺服器最好）。
2. 該頻道 → **編輯頻道** → **整合** → **Webhook** → **新增 Webhook**。
3. 按 **複製 Webhook 網址**，會得到一串像 `https://discord.com/api/webhooks/xxx/yyy` 的網址。
4. 手機記得裝 Discord App 並開啟該頻道的通知。

### 3. 把 Webhook 網址設成 GitHub Secret（不要寫進程式碼！）
在你的 GitHub repo：
**Settings → Secrets and variables → Actions → New repository secret**
- Name：`DISCORD_WEBHOOK_URL`
- Secret：貼上剛剛複製的 Discord Webhook 網址 → **Add secret**

### 4. 啟用並測試
1. 到 repo 的 **Actions** 分頁，若提示要啟用 workflow 就按啟用。
2. 點左側「Accupass 票券監看」→ 右上 **Run workflow** 手動跑一次。
3. 看 log：會印出檢查時間與當前狀態；若當下有票，你的手機 Discord 應該會收到通知。
4. 之後它就會**每 5 分鐘自動跑**，不用你管。

> ⏰ 小提醒：GitHub 排程最短間隔是 5 分鐘，尖峰時段偶爾會延遲幾分鐘，不是秒級監看。

### 5. 搶到票之後 → 記得關掉
到 **Actions** 分頁 → 「Accupass 票券監看」→ 右上 **⋯ → Disable workflow**，
否則它會一直每 5 分鐘提醒你。

---

## 在自己電腦先測試（選用）

```powershell
pip install -r requirements.txt
python -m playwright install chromium
python watch_ticket.py
```

沒設 `DISCORD_WEBHOOK_URL` 的話，腳本會把「原本要推播的訊息」直接印在畫面上，方便你檢查。
若要連手機推播也一起測，先設環境變數再跑：

```powershell
$env:DISCORD_WEBHOOK_URL = "你的 Discord Webhook 網址"
python watch_ticket.py
```

---

## 想改監看的日期 / 場次 / 活動？
打開 `watch_ticket.py` 最上面的「設定區」改這幾個值就好：

| 設定 | 意思 | 範例 |
|---|---|---|
| `EVENT_ID` | 活動 ID（活動網址最後那串數字） | `2605080529051188996723` |
| `TARGET_DAY` | 要在日曆上點的「號數」 | `13` |
| `TARGET_DATE_FRAGMENT` | 用來核對日期切對沒（格式同頁面） | `2026 / 06 / 13` |
| `TARGET_SESSIONS` | 要盯的場次時段（任一釋出就通知） | `["19:40-20:20", "20:30-21:10"]` |

改完存檔 → `git commit` → `git push`，GitHub Actions 下一輪就會用新設定跑。

> ⚠️ 注意：`TARGET_DAY` 目前只支援「跟訂票頁預設同一個月」的日期（這活動都在 6 月所以沒問題）。
> 若要跨月份監看，日曆得多按「上/下個月」，需再加程式碼。