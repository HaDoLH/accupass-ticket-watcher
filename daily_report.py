# -*- coding: utf-8 -*-
"""
每日監看健康報告：分析過去約 30 小時 watch.yml 的執行紀錄，
算出「實際檢查的覆蓋時段、換班次數、最長空窗」，推一則摘要到 Discord。

跑在 GitHub Actions 裡（report.yml），用 GITHUB_TOKEN 讀自己 repo 的執行 log。
"""

import os
import re
import sys
import json
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TW = timezone(timedelta(hours=8))
REPO = os.environ.get("GITHUB_REPOSITORY", "HaDoLH/accupass-ticket-watcher")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
LOOKBACK_HOURS = 30


def gh(args):
    """呼叫 gh CLI，回傳 stdout 字串。"""
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, encoding="utf-8")
    return r.stdout or ""


def fmt(dt: datetime) -> str:
    """格式化成『6/9 22:09』。"""
    return f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}"


def collect_intervals():
    """
    回傳每個監看棒「實際檢查的 (首圈, 末圈, 圈數)」清單，依首圈排序。
    只看 watch.yml、已完成且成功、近 LOOKBACK_HOURS 小時的執行。
    """
    raw = gh(["run", "list", "--repo", REPO, "--workflow", "watch.yml",
              "--limit", "40", "--json", "databaseId,status,conclusion,createdAt"])
    try:
        runs = json.loads(raw or "[]")
    except json.JSONDecodeError:
        runs = []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    intervals = []
    for r in runs:
        if r.get("status") != "completed" or r.get("conclusion") != "success":
            continue
        created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        if created < cutoff:
            continue
        log = gh(["run", "view", str(r["databaseId"]), "--repo", REPO, "--log"])
        # 只抓「檢查圈」那種行（含場次狀態），排除測試通知/banner
        check_ts = []
        for line in log.splitlines():
            if "｜" in line and ("已售完" in line or "可報名" in line or "找不到" in line):
                m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \(UTC\+8\)\]", line)
                if m:
                    check_ts.append(m.group(1))
        if check_ts:
            first = datetime.strptime(check_ts[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW)
            last = datetime.strptime(check_ts[-1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW)
            intervals.append((first, last, len(check_ts)))
    intervals.sort()
    return intervals


def build_message(intervals) -> str:
    now = datetime.now(TW).strftime("%Y-%m-%d %H:%M")
    if not intervals:
        return ("🩺 Accupass 監看健康報告\n"
                f"過去 {LOOKBACK_HOURS} 小時找不到監看檢查紀錄——可能剛部署、或 workflow 被停用。\n"
                f"（{now} 自動回報）")

    cover_start = intervals[0][0]
    cover_end = intervals[-1][1]
    total_checks = sum(n for _, _, n in intervals)

    # 算每次換班的空窗（下一棒首圈 - 上一棒末圈）
    gaps = []
    for i in range(1, len(intervals)):
        sec = (intervals[i][0] - intervals[i - 1][1]).total_seconds()
        gaps.append((intervals[i - 1][1], intervals[i][0], sec))
    max_gap = max((g[2] for g in gaps), default=0)
    big = [g for g in gaps if g[2] > 180]  # 超過 3 分鐘才算「長空窗」

    lines = [
        "🩺 Accupass 監看健康報告",
        f"覆蓋時段：{fmt(cover_start)} ～ {fmt(cover_end)}",
        f"監看棒數：{len(intervals)}（換班 {len(gaps)} 次）｜總檢查 {total_checks} 圈",
    ]
    if big:
        lines.append("⚠️ 發現較長空窗（>3 分鐘）：")
        for s, e, d in big:
            lines.append(f"・{fmt(s)} ～ {fmt(e)}（約 {int(d // 60)} 分鐘沒在跑）")
    else:
        lines.append(f"✅ 全程連續，最長換班空窗僅 {int(max_gap)} 秒")
    lines.append(f"（{now} 自動回報）")
    return "\n".join(lines)


def post_discord(message: str):
    print(message)
    if not WEBHOOK:
        print("（未設定 DISCORD_WEBHOOK_URL，略過推播）")
        return
    data = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "AccupassWatcherReport/1.0 (+https://github.com/HaDoLH/accupass-ticket-watcher)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"✅ 已送出 Discord 報告（HTTP {resp.status}）")


def main():
    intervals = collect_intervals()
    post_discord(build_message(intervals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
