# -*- coding: utf-8 -*-
"""
Accupass 票券監看腳本（雲端排程版 · 特定場次精準監看）

這版會「點進訂票頁、切到指定日期、只盯你要的那幾個場次」，
其中任何一個從『已售完』釋出名額時，就用 Discord Webhook 推播到手機。

設計重點（給之後回來看的自己）：
- Accupass 是 JavaScript 動態渲染網站，原始 HTML 抓不到真實票況，
  所以用 Playwright「無頭瀏覽器」把頁面跑完，再讀畫面真實狀態。
- 真正選「日期＋場次」是在訂票頁 /eflow/ticket/<活動ID>，不是活動主頁。
- 訂票頁每個場次是一張卡片：售完時名稱會帶 sold-out 標記、且顯示「已售完」。
  → 判斷有票 = 該場次「沒有」售完標記也「沒有」已售完文字。
- 這支腳本「跑一次就結束」。每隔幾分鐘重跑交給 GitHub Actions 的 cron。
"""

import os
import sys
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

# 把輸出強制設成 UTF-8，避免 Windows 終端機（預設 cp950）印中文/emoji 時崩潰。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 設定區（要改監看目標就改這裡）──────────────────────────
# 活動 ID（就是活動網址最後那串數字）
EVENT_ID = "2605080529051188996723"

# 訂票頁網址（選日期＋場次的那一頁）
TICKET_URL = f"https://www.accupass.com/eflow/ticket/{EVENT_ID}"

# 要監看的「日期」：TARGET_DAY 是日曆上要點的號數，
# TARGET_DATE_FRAGMENT 用來事後核對日期框真的切對了（格式同頁面：YYYY / MM / DD）。
TARGET_DAY = 13
TARGET_DATE_FRAGMENT = "2026 / 06 / 13"

# 完整場次時間表（時段 → 第幾場次），用來在通知與 log 裡標明場次編號。
SESSION_LABELS = {
    "11:00-11:40": "第1場次",
    "11:50-12:30": "第2場次",
    "12:40-13:20": "第3場次",
    "13:30-14:10": "第4場次",
    "14:20-15:00": "第5場次",
    "15:10-15:50": "第6場次",
    "16:00-16:40": "第7場次",
    "16:50-17:30": "第8場次",
    "18:00-18:40": "第9場次",
    "18:50-19:30": "第10場次",
    "19:40-20:20": "第11場次",
    "20:30-21:10": "第12場次",
    "21:20-22:00": "第13場次",
}

# 要監看的「場次時段」：清單裡任一個釋出名額就通知。
# 目前監看 6/13「整天 13 個場次」（直接拿對照表的全部時段）。
# 想只盯特定幾場，把這行改成例如 ["19:40-20:20", "20:30-21:10"] 即可。
TARGET_SESSIONS = list(SESSION_LABELS.keys())


def label_of(sess: str) -> str:
    """把時段轉成『第N場次（時段）』；對照表沒有的就只顯示時段。"""
    name = SESSION_LABELS.get(sess)
    return f"{name}（{sess}）" if name else sess

# Discord Webhook 網址，從環境變數讀（雲端放 GitHub Secret，本機測試可不設）
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# 測試模式：環境變數 FORCE_TEST=true 時，強制送一則測試通知後就結束，
# 用來確認「Discord → 手機」這條推播路徑是通的（不管票況）。
FORCE_TEST = os.environ.get("FORCE_TEST", "").strip().lower() in ("1", "true", "yes")

# Loop 模式：因為 GitHub 排程實際會被降速到數小時才跑一次，
# 所以改成「單次執行內部自己每 N 秒檢查一圈」，連續跑最多 M 分鐘後結束（交給下一次接力）。
# LOOP_INTERVAL_SECONDS=0 代表只跑一次就結束（本機隨手測時用）。
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default

LOOP_INTERVAL_SECONDS = _int_env("LOOP_INTERVAL_SECONDS", 0)
LOOP_MAX_MINUTES = _int_env("LOOP_MAX_MINUTES", 330)

# 台灣時區（UTC+8）
TW_TZ = timezone(timedelta(hours=8))

# 一個正常瀏覽器的 UA，降低被當機器人的機會
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def now_str() -> str:
    """台灣時間字串，例如 2026-06-04 15:30:12 (UTC+8)"""
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


# 在瀏覽器裡執行的 JS：把日曆上「當月、未停用」的指定號數點下去
JS_CLICK_DAY = """(day) => {
    for (const el of document.querySelectorAll('[class*=calendar-date]')) {
        const t = (el.textContent || '').trim();
        const c = el.className || '';
        if (t === String(day) && !c.includes('is-not-this-month') && !c.includes('disabled')) {
            el.click();
            return true;
        }
    }
    return false;  // 找不到可點的（可能該日不開放或已過期）
}"""

# 在瀏覽器裡執行的 JS：讀出每個場次的「時段名稱」與「是否售完」
JS_READ_SESSIONS = """() => {
    const out = [];
    document.querySelectorAll('p[class*=-name]').forEach(nameEl => {
        const name = nameEl.textContent.trim();
        if (!/\\d{2}:\\d{2}-\\d{2}:\\d{2}/.test(name)) return;  // 只取含時段的場次名稱
        // 售完判斷一：名稱元素帶 sold-out 標記
        const soldByClass = /sold-out/.test(nameEl.className);
        // 售完判斷二：往上找卡片，看有沒有顯示「已售完」的狀態元素
        let statusText = '';
        let n = nameEl;
        for (let i = 0; i < 5 && n; i++) {
            n = n.parentElement;
            if (!n) break;
            const s = n.querySelector('[class*=ticket-selling-status]');
            if (s) { statusText = s.textContent.trim(); break; }
        }
        const soldByText = statusText.includes('售完');
        out.push({ name, soldOut: soldByClass || soldByText, statusText });
    });
    return out;
}"""


def check_on_page(page):
    """
    在既有的 page 上：開訂票頁、切到目標日期、讀場次狀態。
    回傳 (date_value, results, clicked)。讓 loop 能共用同一個瀏覽器、不用每圈重開。
    """
    page.goto(TICKET_URL, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(4_000)  # 等場次卡片渲染

    # 1) 點開日期輸入框（用 JS 點，避免被 sticky 標題列擋住）
    page.eval_on_selector("input[class*=calendar-input]", "el => el.click()")
    page.wait_for_timeout(1_500)

    # 2) 在日曆上點目標號數
    clicked = page.evaluate(JS_CLICK_DAY, TARGET_DAY)
    page.wait_for_timeout(3_500)  # 等切換日期後場次重新載入

    # 3) 讀日期框現在的值，待會核對
    try:
        date_value = page.eval_on_selector("input[class*=calendar-input]", "el => el.value")
    except Exception:
        date_value = ""

    # 4) 讀所有場次狀態
    results = page.evaluate(JS_READ_SESSIONS)
    return date_value, results, clicked


def _post_discord(message: str) -> None:
    """實際把一則訊息送到 Discord Webhook。沒設 Webhook 就只在 terminal 印出。"""
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
        print(f"✅ 已送出 Discord 通知（HTTP {resp.status}）")


def notify_discord(opened_sessions) -> None:
    """把『釋出名額』推播到 Discord。"""
    sessions_text = "\n".join(f"・{label_of(s)}" for s in opened_sessions)
    message = (
        "🎫 釋出名額了！SUPER JUNIOR SJ MARKET\n"
        f"日期：2026/06/13（六）\n"
        f"以下場次目前可報名：\n{sessions_text}\n"
        f"時間：{now_str()}\n"
        f"快去搶 👉 {TICKET_URL}"
    )
    _post_discord(message)


def send_test_notification() -> None:
    """測試模式：送一則測試訊息，確認手機收得到推播。"""
    message = (
        "🔔 這是一則測試通知\n"
        "如果你在手機看到這則，代表 Accupass 票券監看的推播管道正常 👍\n"
        f"監看中：6/13 全部 13 個場次\n"
        f"時間：{now_str()}"
    )
    print(f"[{now_str()}] 🧪 測試模式：送出測試通知")
    _post_discord(message)


def evaluate_and_notify(date_value, results, clicked) -> None:
    """把一次檢查的結果整理、印出 log，並在有場次釋出時推 Discord。"""
    # 核對日期有沒有切對；沒切對就不做判斷（避免拿錯日期誤報）
    if TARGET_DATE_FRAGMENT not in (date_value or ""):
        print(f"[{now_str()}] ⚠️ 日期沒切到 6/13（目前顯示「{date_value}」, 點到日期={clicked}），"
              f"這圈先跳過，等下一圈重試。")
        return

    opened = []          # 已釋出（可報名）的目標場次
    summary = []         # 給 log 看的整體狀態
    for sess in TARGET_SESSIONS:
        hit = next((r for r in results if sess in r["name"]), None)
        if hit is None:
            summary.append(f"{label_of(sess)}=找不到")
        elif hit["soldOut"]:
            summary.append(f"{label_of(sess)}=已售完")
        else:
            summary.append(f"{label_of(sess)}=★可報名★")
            opened.append(sess)

    print(f"[{now_str()}] 日期：{date_value}｜場次狀態：{ '、'.join(summary) }")

    if opened:
        opened_labels = "、".join(label_of(s) for s in opened)
        print(f"[{now_str()}] 🟢 有場次釋出名額：{opened_labels}")
        try:
            notify_discord(opened)
        except Exception as e:
            print(f"[{now_str()}] ⚠️ Discord 推播失敗：{type(e).__name__}: {e}")
    else:
        print(f"[{now_str()}] ⚪ 目標場次目前都還是已售完，繼續監看。")


def run_once(page) -> None:
    """跑一圈檢查＋通知；把例外接住，確保 loop 裡單圈失敗不會中斷整個監看。"""
    try:
        date_value, results, clicked = check_on_page(page)
    except Exception as e:
        print(f"[{now_str()}] ⚠️ 抓取失敗：{type(e).__name__}: {e}")
        return
    evaluate_and_notify(date_value, results, clicked)


def main() -> int:
    # 測試模式：直接送一則測試通知就結束，不去抓票況、也不進 loop
    if FORCE_TEST:
        try:
            send_test_notification()
        except Exception as e:
            print(f"[{now_str()}] ⚠️ 測試通知送出失敗：{type(e).__name__}: {e}")
        return 0

    looping = LOOP_INTERVAL_SECONDS > 0
    print(f"[{now_str()}] 開始監看：{TICKET_URL}")
    print(f"[{now_str()}] 監看 6/13 場次：{ '、'.join(label_of(s) for s in TARGET_SESSIONS) }")
    if looping:
        print(f"[{now_str()}] Loop 模式：每 {LOOP_INTERVAL_SECONDS} 秒檢查一圈，最多連續跑 {LOOP_MAX_MINUTES} 分鐘")

    # 整個監看期間共用同一個瀏覽器（loop 時不用每圈重開，省時省資源）
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)

        deadline = time.monotonic() + LOOP_MAX_MINUTES * 60
        while True:
            run_once(page)

            if not looping:
                break
            # 算好還要不要再跑一圈（留足下一圈的間隔才繼續，否則收工交給下一次接力）
            if time.monotonic() + LOOP_INTERVAL_SECONDS >= deadline:
                print(f"[{now_str()}] ⏱️ 已達連續執行上限，本次結束（交給下一次排程接力）。")
                break
            time.sleep(LOOP_INTERVAL_SECONDS)

        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())