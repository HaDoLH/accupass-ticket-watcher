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

# 要顧的日期（6/10~6/25）；每天全部時段任一釋出就搶
TARGET_YEAR, TARGET_MONTH = 2026, 6
GRAB_DAYS = list(range(10, 26))

LOOP_INTERVAL_SECONDS = int(os.environ.get("LOOP_INTERVAL_SECONDS", "20") or "20")
LOOP_MAX_MINUTES = int(os.environ.get("LOOP_MAX_MINUTES", "330") or "330")
# 測試用：DRY_RUN=1 → 偵測到也只通知、不真的按「立即報名」下單
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

_WEEKDAY_TW = ["一", "二", "三", "四", "五", "六", "日"]

# ── 瀏覽器內 JS ─────────────────────────────────────────
JS_READ_MONTH_TITLE = "() => { const el=document.querySelector('[class*=calendar-title__]'); return el?el.textContent.trim():''; }"
JS_CLICK_MONTH_NAV = "(dir)=>{const iws=[...document.querySelectorAll('[class*=calendar-icon-wrapper]')]; if(!iws.length)return false; const b=dir==='next'?iws[iws.length-1]:iws[0]; if(!b)return false; b.click(); return true;}"
JS_CLICK_DAY = """(day)=>{for(const el of document.querySelectorAll('[class*=calendar-date]')){const t=(el.textContent||'').trim();const c=el.className||'';if(t===String(day)&&!c.includes('is-not-this-month')&&!c.includes('disabled')){el.click();return true;}}return false;}"""
JS_READ_SESSIONS = """()=>{const out=[];document.querySelectorAll('p[class*=-name]').forEach(n=>{const name=n.textContent.trim();if(!/\\d{2}:\\d{2}-\\d{2}:\\d{2}/.test(name))return;const soldClass=/sold-out/.test(n.className);let st='';let x=n;for(let i=0;i<5&&x;i++){x=x.parentElement;if(!x)break;const s=x.querySelector('[class*=ticket-selling-status]');if(s){st=s.textContent.trim();break;}}out.push({name,soldOut:soldClass||st.includes('售完'),status:st});});return out;}"""

# 針對某個「可報名」場次：在它的卡片裡按「+」把數量加到 1。回傳診斷資訊。
JS_SET_QTY_ONE = """(sessTime)=>{
  // 找到名稱含該時段、且「沒有售完」的票卡
  let nameEl=null;
  for(const n of document.querySelectorAll('p[class*=-name]')){
    if(n.textContent.includes(sessTime) && !/sold-out/.test(n.className)){ nameEl=n; break; }
  }
  if(!nameEl) return {ok:false, why:'找不到該場次的可報名卡片'};
  // 往上找票卡容器
  let card=nameEl; for(let i=0;i<6;i++){ if(!card.parentElement)break; card=card.parentElement; if((card.className||'').includes('Ticket-a1a6c1e6')||card.querySelector('button')) break; }
  // 卡片裡所有按鈕，找「+」（加號 / plus / 右邊那顆 stepper）
  const btns=[...card.querySelectorAll('button')];
  const info=btns.map(b=>({t:(b.textContent||'').trim(), cls:b.className, aria:b.getAttribute('aria-label')||''}));
  let plus=btns.find(b=>(b.textContent||'').trim()==='+' || /plus|increase|add|inc\\b/i.test(b.className) || /加|增/.test(b.getAttribute('aria-label')||''));
  if(!plus && btns.length){ plus=btns[btns.length-1]; }  // 退而求其次：最後一顆通常是「+」
  if(!plus) return {ok:false, why:'卡片裡找不到任何按鈕', cardHtml: card.outerHTML.slice(0,1500)};
  plus.click();
  return {ok:true, clicked:{t:(plus.textContent||'').trim(), cls:plus.className}, buttons:info, cardHtml: card.outerHTML.slice(0,1500)};
}"""

# 讀目前數量（看 +1 有沒有生效）
JS_READ_QTY = """(sessTime)=>{
  for(const n of document.querySelectorAll('p[class*=-name]')){
    if(n.textContent.includes(sessTime)){
      let card=n; for(let i=0;i<6;i++){ if(!card.parentElement)break; card=card.parentElement; }
      const q=card.querySelector('[class*=qty]'); return q?q.textContent.trim():'(無qty元素)';
    }
  } return '(找不到場次)';
}"""


def now_str():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


def label_for(day, sess):
    wd = _WEEKDAY_TW[datetime(2026, 6, day).weekday()]
    return f"6/{day}（{wd}） {sess}"


def post_discord(message):
    print(message)
    if not DISCORD_WEBHOOK_URL:
        print("（未設 DISCORD_WEBHOOK_URL，略過推播）")
        return
    data = json.dumps({"content": message}).encode("utf-8")
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
    # 1) 設數量 1
    r = page.evaluate(JS_SET_QTY_ONE, sess)
    print(f"[{now_str()}] 卡位嘗試 {label}｜設數量結果：{json.dumps(r, ensure_ascii=False)[:600]}")
    if not r.get("ok"):
        return False, f"設數量失敗：{r.get('why')}"
    page.wait_for_timeout(800)
    qty = page.evaluate(JS_READ_QTY, sess)
    print(f"[{now_str()}] 目前數量顯示：{qty}")

    # 2) 按底部「立即報名」
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

    # 3) 等待頁面變化，判斷有沒有到「訂單頁/報名表」
    page.wait_for_timeout(3500)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    url = page.url
    body = page.inner_text("body")[:1500]
    print(f"[{now_str()}] 卡位後 URL：{url}")
    # 訂單頁/報名表特徵：URL 變到 order/register，或出現姓名/身分證/報名資料等欄位字樣
    order_signals = ["order", "register", "checkout"]
    form_words = ["報名資料", "姓名", "身分證", "請填寫", "參加者", "聯絡"]
    on_order = any(s in url for s in order_signals) or any(w in body for w in form_words)
    if on_order:
        return True, f"已到訂單頁/報名表（url={url}）"
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


def run_once(page, cycle=0):
    """掃一圈所有日期；偵測到可報名就先通知、再嘗試卡位。回傳 True=已成功卡到（要停）。"""
    t0 = time.monotonic()
    page.goto(TICKET_URL, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(3500)
    days_ok = 0          # 成功切換並讀到場次的天數（用來確認掃描沒壞）
    days_avail = []      # 當圈偵測到「有可報名」的日期
    for day in GRAB_DAYS:
        try:
            val, sessions = switch_to_day(page, day)
        except Exception as e:
            print(f"[{now_str()}] 6/{day} 讀取失敗：{type(e).__name__}: {e}")
            continue
        if not val:
            continue
        days_ok += 1
        avail = [r["name"] for r in sessions if not r["soldOut"]]
        if not avail:
            continue
        days_avail.append(day)
        # 取第一個可報名場次的時段（從名稱抓 HH:MM-HH:MM）
        m = re.search(r"\d{2}:\d{2}-\d{2}:\d{2}", avail[0])
        sess = m.group(0) if m else avail[0]
        label = label_for(day, sess)
        print(f"[{now_str()}] 🟢 偵測到可報名：{label}（該日可報名：{avail}）")
        # 先立刻通知（安全網）
        post_discord(f"🔔 {label} 釋出名額！bot 嘗試自動卡位中…你也可同時手動搶 👉 {TICKET_URL}")
        if DRY_RUN:
            post_discord(f"（DRY_RUN 測試模式：偵測到 {label}，不實際下單）")
            return False
        # 嘗試卡位
        try:
            ok, detail = attempt_grab(page, day, sess)
        except Exception as e:
            ok, detail = False, f"卡位流程例外：{type(e).__name__}: {e}"
        if ok:
            post_discord(f"✅ 已卡到位：{label}！\n10 分鐘內打開 Accupass（同一帳號）接續填資料送出。\n別自己另開新訂單，直接接這筆。\n{TICKET_URL}")
            return True
        else:
            post_discord(f"⚠️ {label} 有票但自動卡位失敗（{detail}），快手動搶 👉 {TICKET_URL}")
            # 不 return，繼續看其他日期
    # 每圈心跳：確認 bot 還活著、掃描沒壞、量得出一圈耗時（不再是黑箱）
    dur = time.monotonic() - t0
    summary = f"可報名日={days_avail}" if days_avail else "全部售完"
    print(f"[{now_str()}] 第 {cycle} 圈完成｜讀到 {days_ok}/{len(GRAB_DAYS)} 天｜耗時 {dur:.0f}s｜{summary}")
    return False


def main():
    if not os.path.exists(STATE_PATH):
        print(f"找不到 {STATE_PATH}（登入狀態）。本機請先跑 export_login.py；雲端請設 ACCUPASS_STATE secret。")
        return 1

    print(f"[{now_str()}] 自動卡位 bot 啟動｜顧 6/{GRAB_DAYS[0]}~6/{GRAB_DAYS[-1]} 全時段｜DRY_RUN={DRY_RUN}")
    deadline = time.monotonic() + LOOP_MAX_MINUTES * 60
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=STATE_PATH, user_agent=UA, locale="zh-TW")
        page = ctx.new_page()

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

        cycle = 0
        while True:
            cycle += 1
            try:
                grabbed = run_once(page, cycle)
            except Exception as e:
                print(f"[{now_str()}] 本圈例外：{type(e).__name__}: {e}")
                grabbed = False
            if grabbed:
                print(f"[{now_str()}] 已卡到位，停止。")
                try:
                    with open("grabbed.flag", "w") as f:
                        f.write("1")  # 記號：已卡到，workflow 不要再接力下一棒
                except Exception:
                    pass
                break
            if time.monotonic() + LOOP_INTERVAL_SECONDS >= deadline:
                print(f"[{now_str()}] 達連續執行上限，本次結束（交給接力）。")
                break
            time.sleep(LOOP_INTERVAL_SECONDS)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())