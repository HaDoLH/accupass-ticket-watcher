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
import re
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

# 本活動的日期都在 2026 年 6 月；換月導航用（把日曆先切到這個年月再點日期）。
# 訂票頁預設可能停在別的月份（例如 5 月），一定要先換到目標月才找得到 6/13、6/14。
TARGET_YEAR = 2026
TARGET_MONTH = 6

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

# ── 監看清單：要盯哪些「日期 + 場次」。任一場釋出名額就通知。──────────
# 每一筆：label=顯示用；day=日曆要點的號數；fragment=核對日期框切對沒（YYYY / MM / DD）；
#         sessions=該日要盯的場次時段（時間用零位補齊 HH:MM 才比得對）。
WATCH_LIST = [
    {
        "label": "6/13（六）",
        "day": 13,
        "fragment": "2026 / 06 / 13",
        # 6/13：13:30 以後的場次，但扣掉已搶到的第8場（16:50-17:30）
        "sessions": [s for s in SESSION_LABELS
                     if s.split("-")[0] >= "13:30" and s != "16:50-17:30"],
    },
    {
        "label": "6/14（日）",
        "day": 14,
        "fragment": "2026 / 06 / 14",
        # 6/14：13:00-16:00 區間＝第4、5、6 場
        "sessions": ["13:30-14:10", "14:20-15:00", "15:10-15:50"],
    },
]


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


# 在瀏覽器裡執行的 JS：讀日曆目前顯示的「月份標題」（例如 "5月 , 2026"）
JS_READ_MONTH_TITLE = """() => {
    const el = document.querySelector('[class*=calendar-title__]');
    return el ? el.textContent.trim() : '';
}"""

# 在瀏覽器裡執行的 JS：點日曆標題列的「上個月 / 下個月」箭頭。
# 標題列有兩個 icon-wrapper：第一個是上個月、最後一個是下個月。
JS_CLICK_MONTH_NAV = """(dir) => {
    const iws = [...document.querySelectorAll('[class*=calendar-icon-wrapper]')];
    if (!iws.length) return false;
    const btn = dir === 'next' ? iws[iws.length - 1] : iws[0];
    if (!btn) return false;
    btn.click();
    return true;
}"""

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


# 英文月名（雲端若用英文語系，月份標題會是 "April , 2026" 這種）
_EN_MONTHS = ["january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december"]


def _parse_month_year(title: str):
    """從日曆標題拆出 (月, 年)，同時支援中文「6月 , 2026」與英文「June , 2026」。"""
    t = (title or "").strip()
    y = re.search(r"(20\d{2})", t)
    year = int(y.group(1)) if y else None
    # 中文：「6月」
    m = re.search(r"(\d{1,2})月", t)
    if m:
        return int(m.group(1)), year
    # 英文：「June」「April」…
    low = t.lower()
    for i, name in enumerate(_EN_MONTHS):
        if name in low:
            return i + 1, year
    return None, year


def _navigate_to_target_month(page) -> str:
    """
    把日曆切到 TARGET_YEAR/TARGET_MONTH（必要時按上/下個月）。
    回傳最後看到的月份標題字串，方便除錯。
    """
    last_title = ""
    for _ in range(24):  # 最多按 24 次，足夠跨好幾個月、避免萬一卡住無限迴圈
        last_title = page.evaluate(JS_READ_MONTH_TITLE) or ""
        cur_month, cur_year = _parse_month_year(last_title)

        # 已經在目標月份就停
        if cur_month == TARGET_MONTH and cur_year == TARGET_YEAR:
            break

        # 判斷要往前還是往後翻；讀不到月份時預設往後翻
        if cur_month and cur_year:
            go_next = (cur_year, cur_month) < (TARGET_YEAR, TARGET_MONTH)
        else:
            go_next = True
        page.evaluate(JS_CLICK_MONTH_NAV, "next" if go_next else "prev")
        page.wait_for_timeout(500)  # 等日曆換月重繪
    return last_title


def check_one_date(page, spec):
    """
    開訂票頁、切到 spec 指定的日期、讀該日場次狀態。
    回傳 (date_value, results, clicked)。一個 spec = 一個日期 + 該日要盯的場次。
    """
    page.goto(TICKET_URL, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(4_000)  # 等場次卡片渲染

    # 1) 點開日期輸入框（用 JS 點，避免被 sticky 標題列擋住）
    page.eval_on_selector("input[class*=calendar-input]", "el => el.click()")
    page.wait_for_timeout(1_200)

    # 2) 先把日曆切到目標月份（訂票頁預設可能停在別的月份）
    month_title = _navigate_to_target_month(page)

    # 3) 在目標月份點目標號數
    clicked = page.evaluate(JS_CLICK_DAY, spec["day"])
    page.wait_for_timeout(3_500)  # 等切換日期後場次重新載入

    # 4) 讀日期框現在的值，待會核對
    try:
        date_value = page.eval_on_selector("input[class*=calendar-input]", "el => el.value")
    except Exception:
        date_value = ""

    # 沒切到目標日期時，把當下月份標題一起印出來，方便除錯
    if spec["fragment"] not in (date_value or ""):
        print(f"[{now_str()}] （除錯）月份標題=「{month_title}」, 日期框=「{date_value}」, 點到日期={clicked}")

    # 5) 讀所有場次狀態
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


def notify_discord(spec, opened_sessions) -> None:
    """把某一天『釋出名額』推播到 Discord。"""
    sessions_text = "\n".join(f"・{label_of(s)}" for s in opened_sessions)
    message = (
        "🎫 釋出名額了！SUPER JUNIOR SJ MARKET\n"
        f"日期：2026/{spec['label']}\n"
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
        f"監看中：{'；'.join(spec['label'] + ' ' + str(len(spec['sessions'])) + '場' for spec in WATCH_LIST)}\n"
        f"時間：{now_str()}"
    )
    print(f"[{now_str()}] 🧪 測試模式：送出測試通知")
    _post_discord(message)


def evaluate_and_notify(spec, date_value, results, clicked) -> None:
    """把一次檢查的結果整理、印出 log，並在有場次釋出時推 Discord。"""
    # 核對日期有沒有切對；沒切對就不做判斷（避免拿錯日期誤報）
    if spec["fragment"] not in (date_value or ""):
        print(f"[{now_str()}] ⚠️ {spec['label']} 日期沒切對（目前顯示「{date_value}」, 點到={clicked}），"
              f"這圈先跳過，等下一圈重試。")
        return

    opened = []          # 已釋出（可報名）的目標場次
    summary = []         # 給 log 看的整體狀態
    for sess in spec["sessions"]:
        hit = next((r for r in results if sess in r["name"]), None)
        if hit is None:
            summary.append(f"{label_of(sess)}=找不到")
        elif hit["soldOut"]:
            summary.append(f"{label_of(sess)}=已售完")
        else:
            summary.append(f"{label_of(sess)}=★可報名★")
            opened.append(sess)

    print(f"[{now_str()}] {spec['label']}｜{date_value}｜{ '、'.join(summary) }")

    if opened:
        opened_labels = "、".join(label_of(s) for s in opened)
        print(f"[{now_str()}] 🟢 {spec['label']} 有場次釋出名額：{opened_labels}")
        try:
            notify_discord(spec, opened)
        except Exception as e:
            print(f"[{now_str()}] ⚠️ Discord 推播失敗：{type(e).__name__}: {e}")
    else:
        print(f"[{now_str()}] ⚪ {spec['label']} 場次目前都還是已售完。")


def _check_and_notify_one(page, spec) -> None:
    """跑一圈檢查＋通知；把例外接住，確保 loop 裡單圈失敗不會中斷整個監看。"""
    try:
        date_value, results, clicked = check_one_date(page, spec)
    except Exception as e:
        print(f"[{now_str()}] ⚠️ 抓取失敗：{type(e).__name__}: {e}")
        return
    evaluate_and_notify(spec, date_value, results, clicked)


def run_once(page) -> None:
    """跑一圈：逐個日期切換、讀取、評估、通知。"""
    for spec in WATCH_LIST:
        _check_and_notify_one(page, spec)


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
    print(f"[{now_str()}] 監看 6/13 場次：{ '、'.join(label_of(s) for s in WATCH_LIST[0]['sessions']) }")
    print(f"[{now_str()}] 監看 6/14 場次：" + "、".join(label_of(s) for s in WATCH_LIST[1]['sessions']))
    if looping:
        print(f"[{now_str()}] Loop 模式：每 {LOOP_INTERVAL_SECONDS} 秒檢查一圈，最多連續跑 {LOOP_MAX_MINUTES} 分鐘")

    # 整個監看期間共用同一個瀏覽器（loop 時不用每圈重開，省時省資源）
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # locale 設繁中：讓雲端（預設英文語系）的日曆月份標題也是「6月 , 2026」而非「June」
        page = browser.new_page(user_agent=UA, locale="zh-TW")

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