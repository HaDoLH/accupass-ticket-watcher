# -*- coding: utf-8 -*-
"""
Accupass 票券監看腳本（雲端排程版）

設計重點（給之後回來看的自己）：
- Accupass 是 JavaScript 動態渲染的網站，原始 HTML 抓不到真實票況，
  所以這裡用 Playwright「無頭瀏覽器」把頁面跑完，再讀「畫面上看得到的文字」來判斷。
- 這支腳本「跑一次就結束」。每隔幾分鐘重跑的工作交給 GitHub Actions 的 cron，
  腳本本身不需要 while 迴圈或 Ctrl+C 處理。
- 有票時用 Discord Webhook 把通知推到手機。
"""

import os
import sys
import json
import urllib.request
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

# 把輸出強制設成 UTF-8，避免 Windows 終端機（預設 cp950）印中文/emoji 時崩潰。
# errors="replace" 代表萬一還是無法顯示，就用替代字元帶過、不要中斷程式。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 設定區 ──────────────────────────────────────────────
# 要監看的活動頁網址
TARGET_URL = "https://www.accupass.com/event/2605080529051188996723"

# Discord Webhook 網址，從環境變數讀（雲端放 GitHub Secret，本機測試可不設）
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# 台灣時區（UTC+8），讓印出的時間戳記是台灣時間
TW_TZ = timezone(timedelta(hours=8))

# 「可以買」的按鈕字樣：頁面可見文字裡只要出現其中任一個，就視為有票可報名。
# 列多個變體是因為 Accupass 不同活動的按鈕字樣可能不同，多列幾個比較保險。
BUYABLE_KEYWORDS = ["立即報名", "我要報名", "立即購票", "我要購票", "馬上報名", "報名參加"]

# 「不算有票」的狀態字樣：純粹用來在 log 裡顯示頁面目前有哪些狀態，方便除錯。
STATUS_KEYWORDS = ["已售完", "售完", "已截止", "報名截止", "即將開賣", "尚未開賣", "暫停"]


def now_str() -> str:
    """回傳台灣時間的字串，例如 2026-06-04 15:30:12 (UTC+8)"""
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


def fetch_visible_text() -> str:
    """
    用無頭瀏覽器打開頁面、等 JS 跑完，回傳「畫面上看得到的文字」。
    這段文字不含 <script> 裡的 i18n 翻譯字典，所以關鍵字判斷才乾淨可靠。
    """
    with sync_playwright() as p:
        # headless=True 代表背景執行、不開視窗
        browser = p.chromium.launch(headless=True)
        # 帶一個正常的瀏覽器 UA，減少被當成機器人的機會
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        )
        # 進到頁面，等到網路大致閒置（JS 載完）；最多等 60 秒
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60_000)
        # 再多給一點時間讓票券區塊渲染出來
        page.wait_for_timeout(3_000)
        text = page.inner_text("body")
        browser.close()
        return text


def analyze(text: str):
    """
    分析可見文字，回傳 (是否有票, 找到的可買關鍵字清單, 找到的狀態字清單)。
    判斷規則：可見文字含任一個「可以買」的按鈕字樣 → 有票。
    """
    found_buyable = [k for k in BUYABLE_KEYWORDS if k in text]
    found_status = [k for k in STATUS_KEYWORDS if k in text]
    has_ticket = len(found_buyable) > 0
    return has_ticket, found_buyable, found_status


def notify_discord(found_buyable) -> None:
    """把「有票了！」推播到 Discord。沒設定 Webhook 就只在 terminal 提醒。"""
    message = (
        "🎫 **有票了！Accupass 偵測到可報名的票券**\n"
        f"偵測到的按鈕：{ '、'.join(found_buyable) }\n"
        f"時間：{ now_str() }\n"
        f"快去搶 👉 {TARGET_URL}"
    )

    if not DISCORD_WEBHOOK_URL:
        print("⚠️  尚未設定 DISCORD_WEBHOOK_URL，略過手機推播（以下為原本要送出的訊息）：")
        print(message)
        return

    data = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            # Discord（前面的 Cloudflare）會擋掉沒有正常 User-Agent 的請求（回 403），
            # 所以一定要帶一個 UA，否則推播會失敗。
            "User-Agent": "AccupassTicketWatcher/1.0 (+https://github.com/HaDoLH/accupass-ticket-watcher)",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        # Discord 成功會回 204 No Content
        print(f"✅ 已送出 Discord 通知（HTTP {resp.status}）")


def main() -> int:
    print(f"[{now_str()}] 開始檢查：{TARGET_URL}")
    try:
        text = fetch_visible_text()
    except Exception as e:
        # 網路錯誤、渲染逾時等都接住，印出原因後正常結束（交給 cron 下一輪重試）
        print(f"[{now_str()}] ⚠️ 抓取失敗：{type(e).__name__}: {e}")
        return 0

    has_ticket, found_buyable, found_status = analyze(text)

    status_desc = "、".join(found_status) if found_status else "（無）"
    print(f"[{now_str()}] 頁面狀態字：{status_desc}")

    if has_ticket:
        print(f"[{now_str()}] 🟢 有票！偵測到按鈕：{ '、'.join(found_buyable) }")
        try:
            notify_discord(found_buyable)
        except Exception as e:
            print(f"[{now_str()}] ⚠️ Discord 推播失敗：{type(e).__name__}: {e}")
    else:
        print(f"[{now_str()}] ⚪ 目前沒有可報名的票，繼續監看。")

    return 0


if __name__ == "__main__":
    sys.exit(main())