# Accupass 票券監看 / 卡位 bot 🎫

偵測 Accupass 活動「**釋出名額**」（有人退票、從售完變回可報名），第一時間用 **Discord @everyone**
通知你去手動搶。跑在 GitHub 雲端、**不用開電腦**。

> 🤖 **給接手的 AI / 未來的我**：先讀這份，別重蹈覆轍。
> - 真正在跑的 bot 是 **`auto_grab.py` + `.github/workflows/auto_grab.yml`**，**不是** `watch.yml`。
> - `watch.yml`（舊監看）與健康日報**已停用**，**不要重新啟用**（會跟新 bot 互相 cancel 打架）。
> - 登入 cookie 在**雲端 IP 仍有效**（已多次實測、bot 真的卡到過訂單頁）。**別誤判成「cookie 雲端失效」**。
> - 改 `.py` / `.yml` 後要 `git push`；要讓雲端吃到新碼，需 **cancel 舊棒再 `gh workflow run`**。
> - 細節全在 [`PLAYBOOK.md`](PLAYBOOK.md)。

---

## 現在的狀態

| Workflow | 狀態 | 說明 |
|---|---|---|
| **Accupass 自動卡位**（`auto_grab.yml`） | ✅ active、持續跑 | 唯一的偵測+通知來源 |
| Accupass 票券監看（`watch.yml`） | ⛔ 已停用 | 舊版、慢、會打架，別開 |
| 監看健康日報（`report.yml`） | ⛔ 已停用 | 會發假警報，別開 |

**現行模式 = `NOTIFY_ONLY`（只通知、不自動下單）**：偵測到釋出就 @everyone 叫你，
你自己在手機（同一個 FB 帳號）手動搶。這是最穩、零封帳號風險的玩法。

---

## 運作架構

```
Accupass API 偵測（~1 秒掃完所有日期的票況）
        │   GetEventTickets：一個請求回傳全場次 soldCount/ticketCount/status
        ▼
GitHub Actions（內部每 3 秒一圈 loop + PAT 自我接力 + 排程安全網 → 近 24h 不停）
        │
   偵測到「可報名」（soldCount < ticketCount 且 status 非售完/過期）
        ▼
Discord Webhook ──► @everyone 大標題通知 📱 ──► 你手動搶
```

為什麼用 API 不用「瀏覽器點日曆」：點日曆掃 16 天要 ~64 秒/圈，API 一個請求 ~1 秒就拿到全部。詳見 PLAYBOOK。

---

## 檔案地圖

| 檔案 | 用途 |
|---|---|
| `auto_grab.py` | **主程式**：API 偵測 → 通知（或可選自動卡位） |
| `.github/workflows/auto_grab.yml` | 雲端排程：loop + PAT 接力 + 排程安全網 |
| `export_login.py` | 本機跑一次、手動登入、匯出 `state.json` 登入狀態 |
| `requirements.txt` | 相依套件（playwright） |
| `state.json` | 🔒 你的登入 cookie，**本機限定、已 gitignore**，絕不外流 |
| `probe_*.py` | 🔒 本機逆向/除錯腳本（已 gitignore） |

---

## 換一個新的 Accupass 活動（最快複製）

1. **改活動**：`auto_grab.py` 最上面把 `EVENT_ID` 換成新活動 ID（活動網址最後那串數字）；
   需要的話也調 `GRAB_DAYS`（要顧的日期，預設 `range(10, 26)`）。
2. **重匯登入**：本機 `python export_login.py`（會開瀏覽器讓你登入）→ 產生 `state.json` →
   `gh secret set ACCUPASS_STATE < state.json`。
3. **設通知**：`gh secret set DISCORD_WEBHOOK_URL`（值 = 你的 Discord webhook 網址）。
4. **啟動**：`gh workflow run auto_grab.yml -f dry_run=false`。

> secrets 三件套：`DISCORD_WEBHOOK_URL`、`ACCUPASS_STATE`、`WATCHER_PAT`（自我接力用）。

---

## 啟動 / 停止

```bash
# 啟動（重啟，吃到最新程式碼）
gh run cancel <還在跑的舊棒ID>            # 先停舊棒
gh workflow run auto_grab.yml --repo HaDoLH/accupass-ticket-watcher -f dry_run=false

# 看狀態
gh run list --repo HaDoLH/accupass-ticket-watcher --workflow auto_grab.yml --limit 5

# 完全停止 → 到 GitHub Actions 把「Accupass 自動卡位」Disable
```

切換「只通知 / 自動卡位」：改 `auto_grab.yml` 裡的 `NOTIFY_ONLY`（`1`=只通知、`0`=自動卡位），push 後重啟。

---

## 想了解全部細節 / 換別的售票平台（KKTIX、拓元…）？

👉 看 **[`PLAYBOOK.md`](PLAYBOOK.md)**：完整架構原理、逆向別的平台 API 的方法論、
這次踩過的所有坑與解法、做不到的物理極限、部署維運 SOP。