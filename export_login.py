# -*- coding: utf-8 -*-
"""
匯出 Accupass 登入狀態（給自動搶用）。

用法（在你自己的終端機跑，會跳出一個瀏覽器視窗）：
    python export_login.py

流程：
1. 自動打開瀏覽器到 Accupass 登入頁。
2. 你「手動登入」（Google / Facebook / Email 都行）。
3. 登入成功、看到自己頭像後，回到終端機按 Enter。
4. 程式把登入狀態存成 state.json（裡面是你的登入 Cookie）。

⚠️ state.json = 你的登入通行證，絕對不要上傳/外流；本專案 .gitignore 已忽略它。
"""

import sys
from playwright.sync_api import sync_playwright

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

OUT = "state.json"

with sync_playwright() as p:
    # headless=False → 會跳出看得到的瀏覽器視窗讓你登入
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(locale="zh-TW")
    page = ctx.new_page()
    page.goto("https://www.accupass.com/login", wait_until="domcontentloaded")

    print("\n" + "=" * 50)
    print("請在剛跳出的瀏覽器視窗裡【手動登入】Accupass")
    print("（Google / Facebook / Email 都可以）")
    print("登入成功、看到自己的頭像後，回到這裡按 Enter")
    print("=" * 50)
    input("登入好了就按 Enter ▶ ")

    ctx.storage_state(path=OUT)
    browser.close()
    print(f"\n✅ 已存出登入狀態到 {OUT}")
    print("（這就是你的登入通行證，等下會設成 GitHub Secret，不要外流）")