# -*- coding: utf-8 -*-
"""
Accupass 自動卡位 bot（race-to-hold）。

設計（與使用者談定）：
- bot 只負責「搶到卡位」——偵測到某場釋出 → 用你的登入狀態，點該場次、數量設 1、
  按「立即報名」衝到訂單頁（把位子鎖住約 10 分鐘）→ Discord 通知你。
- **不填任何個資**（姓名/身分證等你自己在手機接續填）→ 隱私資料完全不上雲端。

安全機制：
- 一偵測到釋出，**先立刻 Discord 通知**（就算自動卡位失敗，你也能馬上手動搶）。
- 卡位成功 → 通知「已卡到」並停止（不重複下單）。
- 卡位失敗 → 通知「有票但自動卡位失敗，快手動搶」＋把當下結構印進 log 方便我修。

登入狀態：讀 state.json（Playwright storageState）。雲端由 workflow 把 ACCUPASS_STATE
secret 寫成 state.json。**state.json 不進 git**。
"""

import os
import re
import sys
import json
import time
import urllib.request
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 設定 ───────────────────────────────────────────────
EVENT_ID = "2605080529051188996723"
TICKET_URL = f"https://www.accupass.com/eflow/ticket/{EVENT_ID}"
STATE_PATH = "state.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
TW_TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 要顧的日期；每天全部時段任一釋出就搶。
# 改用 API 偵測後，一次掃完所有日期只要 1~2 秒，不必再為了速度縮範圍，
# 所以顧回完整 6/10~6/25（哪天釋出都抓得到）。
TARGET_YEAR, TARGET_MONTH = 2026, 6
GRAB_DAYS = list(range(10, 26))
DAYS_LABEL = f"6/{GRAB_DAYS[0]}~6/{GRAB_DAYS[-1]}"

# Accupass 票況查詢 API（eflow-queue）：一次回傳整個活動所有場次的票況。
QUEUE_BASE = "https://eflow-queue.accupass.com/api"

LOOP_INTERVAL_SECONDS = int(os.environ.get("LOOP_INTERVAL_SECONDS", "20") or "20")
LOOP_MAX_MINUTES = int(os.environ.get("LOOP_MAX_MINUTES", "330") or "330")
# 測試用：DRY_RUN=1 → 偵測到也只通知、不真的按「立即報名」下單
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
# 只通知模式：偵測到釋出就 @everyone 叫你手動搶，bot 完全不碰下單動作（降低封帳號風險）
NOTIFY_ONLY = os.environ.get("NOTIFY_ONLY", "").strip().lower() in ("1", "true", "yes")

_WEEKDAY_TW = ["一", "二", "三", "四", "五", "六", "日"]

# ── 瀏覽器內 JS ─────────────────────────────────────────
JS_READ_MONTH_TITLE = "() => { const el=document.querySelector('[class*=calendar-title__]'); return el?el.textContent.trim():''; }"
JS_CLICK_MONTH_NAV = "(dir)=>{const iws=[...document.querySelectorAll('[class*=calendar-icon-wrapper]')]; if(!iws.length)return false; const b=dir==='next'?iws[iws.length-1]:iws[0]; if(!b)return false; b.click(); return true;}"
JS_CLICK_DAY = """(day)=>{for(const el of document.querySelectorAll('[class*=calendar-date]')){const t=(el.textContent||'').trim();const c=el.className||'';if(t===String(day)&&!c.includes('is-not-this-month')&&!c.includes('disabled')){el.click();return true;}}return false;}"""
JS_READ_SESSIONS = """()=>{const out=[];document.querySelectorAll('p[class*=-name]').forEach(n=>{const name=n.textContent.trim();if(!/\\d{2}:\\d{2}-\\d{2}:\\d{2}/.test(name))return;const soldClass=/sold-out/.test(n.className);let st='';let x=n;for(let i=0;i<5&&x;i++){x=x.parentElement;if(!x)break;const s=x.querySelector('[class*=ticket-selling-status]');if(s){st=s.textContent.trim();break;}}out.push({name,soldOut:soldClass||st.includes('售完'),status:st});});return out;}"""

# 針對某個「可報名」場次：在它的卡片裡按「+」把數量加到 1。回傳診斷資訊。
JS_SET_QTY_ONE = """(sessTime)=>{
  let nameEl=null;
  for(const n of document.querySelectorAll('p[class*=-name]')){
    if(n.textContent.includes(sessTime)){ nameEl=n; break; }
  }
  if(!nameEl) return {ok:false, why:'找不到該場次卡片'};
  // 往上找到「含 +/- 數量控制」的卡片容器
  let card=nameEl;
  for(let i=0;i<8;i++){ if(!card.parentElement)break; card=card.parentElement; if(card.querySelector('[class*=change-qty]')||card.querySelector('[class*=-add]'))break; }
  // Accupass 的「+」是 span（class 含 -add），不是 button
  const add=card.querySelector('[class*=change-qty] [class*=-add]')||card.querySelector('[class*=-add]');
  if(!add) return {ok:false, why:'找不到加號(+)控制', cardHtml: card.outerHTML.slice(0,1500)};
  add.click();
  return {ok:true, clicked: add.className, cardHtml: card.outerHTML.slice(0,1200)};
}"""

# 讀目前數量（看 +1 有沒有生效）：找卡片裡顯示「x數字」的那個 span
JS_READ_QTY = """(sessTime)=>{
  for(const n of document.querySelectorAll('p[class*=-name]')){
    if(n.textContent.includes(sessTime)){
      let card=n; for(let i=0;i<8;i++){ if(!card.parentElement)break; card=card.parentElement; if(card.querySelector('[class*=change-qty]'))break; }
      const sp=[...card.querySelectorAll('span')].find(s=>/x\\s*\\d+/.test(s.textContent||''));
      return sp?sp.textContent.trim():'(無qty元素)';
    }
  } return '(找不到場次)';
}"""


def now_str():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


def label_for(day, sess):
    wd = _WEEKDAY_TW[datetime(2026, 6, day).weekday()]
    return f"6/{day}（{wd}） {sess}"


def post_discord(message, ping=False):
    print(message)
    if not DISCORD_WEBHOOK_URL:
        print("（未設 DISCORD_WEBHOOK_URL，略過推播）")
        return
    payload = {"content": message}
    if ping:
        # 允許 @everyone 真的觸發通知（私人頻道、tag 自己提醒用）
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "AccupassAutoGrab/1.0 (+https://github.com/HaDoLH/accupass-ticket-watcher)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  已送出 Discord（HTTP {resp.status}）")
    except Exception as e:
        print(f"  Discord 推播失敗：{type(e).__name__}: {e}")


_EN = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
def _parse_my(t):
    t = (t or '').strip(); y = re.search(r"(20\d{2})", t); yr = int(y.group(1)) if y else None
    m = re.search(r"(\d{1,2})月", t)
    if m:
        return int(m.group(1)), yr
    low = t.lower()
    for i, nm in enumerate(_EN):
        if nm in low:
            return i + 1, yr
    return None, yr


def nav_month(page):
    for _ in range(24):
        cm, cy = _parse_my(page.evaluate(JS_READ_MONTH_TITLE) or '')
        if cm == TARGET_MONTH and cy == TARGET_YEAR:
            break
        go = 'next' if (not cm or not cy or (cy, cm) < (TARGET_YEAR, TARGET_MONTH)) else 'prev'
        page.evaluate(JS_CLICK_MONTH_NAV, go)
        page.wait_for_timeout(400)


def switch_to_day(page, day):
    """在已開的 eflow 頁切到指定日期，回傳 (date_value, sessions) 或 (None,[])。"""
    page.eval_on_selector("input[class*=calendar-input]", "el=>el.click()")
    page.wait_for_timeout(700)
    nav_month(page)
    page.evaluate(JS_CLICK_DAY, day)
    page.wait_for_timeout(2200)
    try:
        val = page.eval_on_selector("input[class*=calendar-input]", "el=>el.value")
    except Exception:
        val = ""
    if f"/ {day:02d}" not in (val or ""):
        return None, []
    return val, page.evaluate(JS_READ_SESSIONS)


def attempt_grab(page, day, sess):
    """偵測到 day 的 sess 可報名 → 嘗試衝到訂單頁卡位。回傳 (success, detail)。"""
    label = label_for(day, sess)
    # 1) 設數量 1：點該場次卡片裡的「+」。Accupass 的 +/- 是 span（非 button），用真實點擊最穩。
    set_detail = ""
    try:
        name = page.locator("p[class*=-name]", has_text=sess).first
        card = name.locator("xpath=ancestor::div[.//*[contains(@class,'change-qty')]][1]")
        card.locator("[class*='change-qty'] [class*='-add']").first.click(timeout=5000)
    except Exception as e:
        set_detail = f"真實點+失敗（{type(e).__name__}）→ 改用 JS 後援"
        try:
            r = page.evaluate(JS_SET_QTY_ONE, sess)
            if not r.get("ok"):
                return False, f"設數量失敗：{r.get('why')}"
        except Exception as e2:
            return False, f"設數量例外：{type(e2).__name__}: {e2}"
    page.wait_for_timeout(700)
    qty = page.evaluate(JS_READ_QTY, sess)
    print(f"[{now_str()}] 卡位嘗試 {label}｜數量顯示：{qty}｜{set_detail or '已點+'}")
    if "x0" in (qty or "") or not re.search(r"x\s*[1-9]", qty or ""):
        return False, f"數量沒加成功（顯示 {qty}）"

    # 2) 按底部「立即報名」。數量剛變 1，按鈕可能要一下才啟用 → 多等一點再點。
    page.wait_for_timeout(600)
    clicked = False
    for sel in ["a:has-text('立即報名')", "button:has-text('立即報名')", "text=立即報名"]:
        try:
            page.locator(sel).last.click(timeout=4000)
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        return False, "找不到/點不到『立即報名』按鈕"

    # 3) 逐秒等頁面跳到訂單頁（最多 ~12 秒，跳頁稍慢也不誤判失敗）
    def _is_order_page():
        u = page.url
        # 訂單頁特徵：URL 從 /eflow/ticket/<id> 變成 /eflow/<orderId>（不含 order/register 字樣）
        if "/eflow/" in u and "/ticket/" not in u:
            return True, u
        try:
            b = page.inner_text("body")[:2000]
        except Exception:
            b = ""
        if any(w in b for w in ["報名資料", "填寫資料", "身分證", "參加者", "出生年月日", "未完成訂單將自動取消"]):
            return True, u
        return False, u

    url = page.url
    for _ in range(12):
        page.wait_for_timeout(1000)
        ok, url = _is_order_page()
        if ok:
            print(f"[{now_str()}] 卡位後 URL：{url}")
            return True, f"已到訂單頁/報名表（url={url}）"
    print(f"[{now_str()}] 卡位後 URL：{url}")
    return False, f"按了立即報名但沒進到訂單頁（url={url}）"


def verify_login(page):
    """啟動時確認登入是否有效（首頁無登入按鈕＋有社群頭像）。回傳 (ok, 說明)。"""
    page.goto("https://www.accupass.com/", wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(3000)
    body = page.inner_text("body")
    top = body.split("本週")[0] if "本週" in body else body[:300]
    has_login_btn = ("登入" in top) or ("註冊" in top)
    avatar = page.evaluate(
        "()=>{const i=[...document.querySelectorAll('header img,[class*=header] img,[class*=avatar] img')];"
        "return i.some(x=>/graph.facebook|googleusercontent|fbcdn|avatar|gravatar/i.test(x.src||''));}")
    ok = (not has_login_btn) and bool(avatar)
    return ok, f"登入按鈕={'有' if has_login_btn else '無'}, 社群頭像={'有' if avatar else '無'}"


# ── API 偵測（取代逐日點日曆，快 ~50 倍）────────────────────
# 啟動時攔下網站自己發的 authorization 標頭，重複用（不必碰登入 token 明文）。
_auth = {"header": None}
# 查詢用的短效 token（GetOrRenewToken 給的，約 11 分鐘有效）
_token = {"value": None, "exp": 0.0}
# 已 @everyone 通知過的場次（同一場只 tag 一次，避免洗頻）
_notified = set()
# 卡到後冷卻：期間不再搶，給使用者時間去完成那筆訂單（比 10 分 hold 多一點，避免新訂單擠掉舊的）
GRAB_COOLDOWN_SECONDS = 12 * 60
_cooldown = {"until": 0.0}


def _on_queue_request(req):
    if "eflow-queue.accupass.com" in req.url:
        a = req.headers.get("authorization")
        if a and not _auth["header"]:
            _auth["header"] = a


def capture_auth(page):
    """開票券頁，攔下 authorization 標頭。回傳是否攔到。"""
    page.goto(TICKET_URL, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(4000)
    return bool(_auth["header"])


def api_renew_token(api):
    """POST GetOrRenewToken 取得查詢 token。回傳 (ok, isCanOrder, msg)。"""
    if not _auth["header"]:
        return False, False, "尚未取得 authorization 標頭"
    hdr = {"authorization": _auth["header"], "content-type": "application/json"}
    try:
        r = api.post(f"{QUEUE_BASE}/GetOrRenewToken?EventIdNumber={EVENT_ID}", headers=hdr, data="{}")
        j = r.json()
        _token["value"] = j.get("token")
        _token["exp"] = time.monotonic() + 9 * 60  # 提早 2 分鐘續，保險
        return bool(_token["value"]), bool(j.get("isCanOrder")), j.get("customMessage", "")
    except Exception as e:
        return False, False, f"{type(e).__name__}: {e}"


def api_scan(api):
    """用 API 掃全部場次票況。回傳 (available, ok, err)。
    available = [{name, day, sess, sold, total}]（限 GRAB_DAYS、非 Expired/SoldOut、sold<total）。"""
    hdr = {"authorization": _auth["header"], "content-type": "application/json"}
    if not _token["value"] or time.monotonic() > _token["exp"]:
        ok, _, msg = api_renew_token(api)
        if not ok:
            return [], False, f"取 token 失敗：{msg}"
    try:
        # token 含 + / = 等字元，放進網址必須 URL 編碼（safe='' 連 / 也編）
        url = f"{QUEUE_BASE}/GetEventTickets?eventIdNumber={EVENT_ID}&token={quote(_token['value'], safe='')}"
        r = api.get(url, headers=hdr)
        if r.status != 200:  # token 可能失效 → 續一次再試
            ok, _, msg = api_renew_token(api)
            if not ok:
                return [], False, f"重取 token 失敗：{msg}"
            url = f"{QUEUE_BASE}/GetEventTickets?eventIdNumber={EVENT_ID}&token={quote(_token['value'], safe='')}"
            r = api.get(url, headers=hdr)
        if r.status != 200:
            return [], False, f"GetEventTickets HTTP {r.status}"
        lst = r.json().get("eventTicketList", [])
    except Exception as e:
        return [], False, f"{type(e).__name__}: {e}"

    avail = []
    for t in lst:
        st = t.get("ticketStatus")
        sold = t.get("soldCount") or 0
        total = t.get("ticketCount") or 0
        if st in ("Expired", "SoldOut") or sold >= total:
            continue
        name = t.get("name", "")
        md = re.search(r"2026/06/(\d{2})", name)
        ms = re.search(r"\d{2}:\d{2}-\d{2}:\d{2}", name)
        if not md or not ms:
            continue
        day = int(md.group(1))
        if day not in GRAB_DAYS:
            continue
        avail.append({"name": name, "day": day, "sess": ms.group(0), "sold": sold, "total": total})
    return avail, True, ""


def run_once(page, api, cycle=0):
    """用 API 掃一圈全部日期；偵測到可報名就先通知、再用瀏覽器嘗試卡位。
    一律回傳 False（卡到也不停止：設冷卻後繼續顧，給使用者時間接手完成）。"""
    t0 = time.monotonic()
    avail, ok, err = api_scan(api)
    dur = time.monotonic() - t0

    if not ok:
        print(f"[{now_str()}] 第 {cycle} 圈｜API 掃描失敗：{err}｜耗時 {dur:.1f}s")
        return False
    if not avail:
        # 每圈心跳：確認還活著、量得出回訪速度（API 版一圈約 1~2 秒）
        print(f"[{now_str()}] 第 {cycle} 圈完成｜API 掃 {DAYS_LABEL} 全場｜耗時 {dur:.1f}s｜全部售完")
        return False

    # 有可報名！先處理第一筆（通常一次只釋一兩個）
    target = avail[0]
    day, sess = target["day"], target["sess"]
    label = label_for(day, sess)
    print(f"[{now_str()}] 🟢 偵測到可報名：{label}（{target['sold']}/{target['total']}）｜本圈可報名：{[a['name'] for a in avail]}")
    # 同一場只 @everyone 一次，避免反覆偵測時每幾秒洗一次頻
    first_time = label not in _notified
    _notified.add(label)

    # 只通知模式：叫你手動搶，bot 不下單
    if NOTIFY_ONLY:
        if first_time:
            remain = (target["total"] or 0) - (target["sold"] or 0)
            post_discord(
                f"@everyone\n"
                f"# 🔔 6/{day} {sess}\n"          # 大標題：一眼看到要選哪天哪場
                f"**日曆就選這天這場！** 剩 {remain} 位\n"
                f"開 Accupass（同 FB 帳號）→ 日曆選 **6/{day}** → 點這場 → 按「+」→ 立即報名\n"
                f"⏱️ 10 分鐘內完成 👉 {TICKET_URL}",
                ping=True)
        return False

    # ── 自動卡位模式 ──
    # 卡到後冷卻：給你時間去完成那筆，期間不再搶（免得新訂單把你正在填的擠掉）
    if time.monotonic() < _cooldown["until"]:
        mins = int((_cooldown["until"] - time.monotonic()) / 60) + 1
        print(f"[{now_str()}] 偵測到 {label} 可報名，但卡位冷卻中（約 {mins} 分後恢復），本圈不搶")
        return False

    # 先立刻通知（安全網：就算自動卡位失敗，你也能馬上手動搶）
    if first_time:
        post_discord(f"@everyone\n🔔 {label} 釋出名額！bot 嘗試自動卡位中…你也可同時手動搶 👉 {TICKET_URL}", ping=True)
    if DRY_RUN:
        if first_time:
            post_discord(f"（DRY_RUN 測試模式：偵測到 {label}，不實際下單）")
        return False

    # 用瀏覽器衝到報名頁卡位（這段在搶到時才跑一次）
    order_url = ""
    try:
        page.goto(TICKET_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(1500)
        switch_to_day(page, day)
        grabbed, detail = attempt_grab(page, day, sess)
        order_url = page.url  # 卡到後的「專屬訂單網址」
    except Exception as e:
        grabbed, detail = False, f"卡位流程例外：{type(e).__name__}: {e}"
    if grabbed:
        # 卡到位最重要 → 一定 @everyone。給兩條接手路：我的訂單 + 直接訂單連結
        post_discord(
            f"@everyone\n"
            f"# ✅ 已卡到位：{label}\n"
            f"**10 分鐘內完成**，兩條路擇一接手：\n"
            f"① 打開 Accupass →「**我的訂單／報名紀錄**」→ 那筆未完成的 → 接著填送出\n"
            f"② 或直接點這筆訂單連結 👉 {order_url}\n"
            f"⚠️ 別自己另開新報名，直接接這筆。",
            ping=True)
        # 不停止！設冷卻，給你 12 分鐘去接手完成；冷卻過了沒接成就繼續搶下一張
        _cooldown["until"] = time.monotonic() + GRAB_COOLDOWN_SECONDS
        return False
    if first_time:
        post_discord(f"@everyone\n⚠️ {label} 有票但自動卡位失敗（{detail}），快手動搶 👉 {TICKET_URL}", ping=True)
    return False


def main():
    if not os.path.exists(STATE_PATH):
        print(f"找不到 {STATE_PATH}（登入狀態）。本機請先跑 export_login.py；雲端請設 ACCUPASS_STATE secret。")
        return 1

    mode = "只通知不下單" if NOTIFY_ONLY else ("DRY 測試" if DRY_RUN else "自動卡位")
    print(f"[{now_str()}] bot 啟動｜API 偵測 {DAYS_LABEL} 全時段｜模式={mode}")
    deadline = time.monotonic() + LOOP_MAX_MINUTES * 60
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=STATE_PATH, user_agent=UA, locale="zh-TW")
        page = ctx.new_page()
        page.on("request", _on_queue_request)  # 攔 authorization 標頭給 API 用
        api = ctx.request                       # 共用 cookie 的 HTTP client

        # 啟動先驗證登入（尤其雲端 IP 下 cookie 還有沒有效）
        try:
            ok, why = verify_login(page)
            print(f"[{now_str()}] 登入檢查：{'有效✅' if ok else '失效⚠️'}（{why}）")
            if not ok:
                post_discord(f"⚠️ 自動卡位 bot：登入狀態在此環境**失效**（{why}）。"
                             f"→ 仍會偵測釋出並通知你手動搶，但無法自動卡位。"
                             f"（可能 cookie 綁 IP；雲端失效的話需改別的跑法）")
        except Exception as e:
            print(f"[{now_str()}] 登入檢查例外：{type(e).__name__}: {e}")

        # 開票券頁攔下 authorization 標頭（API 偵測必需）
        try:
            got = capture_auth(page)
            print(f"[{now_str()}] 攔截 API 授權標頭：{'成功✅' if got else '失敗⚠️'}")
            if not got:
                post_discord("⚠️ 自動卡位 bot：攔不到 API 授權標頭，無法用快速偵測。請通知我檢查。")
        except Exception as e:
            print(f"[{now_str()}] 攔截授權標頭例外：{type(e).__name__}: {e}")

        cycle = 0
        while True:
            cycle += 1
            try:
                run_once(page, api, cycle)  # 卡到也不停（內部設冷卻），持續顧
            except Exception as e:
                print(f"[{now_str()}] 本圈例外：{type(e).__name__}: {e}")
            if time.monotonic() + LOOP_INTERVAL_SECONDS >= deadline:
                print(f"[{now_str()}] 達連續執行上限，本次結束（交給接力）。")
                break
            time.sleep(LOOP_INTERVAL_SECONDS)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())