# -*- coding: utf-8 -*-
"""NapCat 掉线看门狗：连续 N 分钟连不上 OneBot 端口就弹 Windows 通知。

为什么要有它：NapCat 掉登录态时会退回扫码界面，OneBot WS 端口不再监听，
qq_watch 只会安静地重连（日志里每分钟一条 ClientConnectorError），
人不在电脑前就完全不知道要扫码。这个脚本只做一件事：发现持续不通 → 弹窗提醒。

用法：
  python napcat_alive.py                  # 常驻，默认 30s 探一次、连续 5 分钟不通就通知
  python napcat_alive.py --once           # 只探一次并打印结果（不通知）
  python napcat_alive.py --port 3001 --minutes 5 --interval 30
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(r"C:\Users\yourname\.dsh\qq-duty")
LOG = HOME / "napcat_alive.log"
STATE = HOME / "napcat_alive.json"

DEFAULT_PORTS = [3001, 3002]          # 3001=机器人号，3002=主人号（自己用）
EXE = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def probe(port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def toast(title: str, body: str) -> bool:
    """用 PowerShell + WinRT 弹一条 Windows 通知（不依赖第三方库）。"""
    ps = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$tpl = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>__TITLE__</text>
      <text>__BODY__</text>
    </binding>
  </visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($tpl)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""
    ps = ps.replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;"))
    ps = ps.replace("__BODY__", body.replace("&", "&amp;").replace("<", "&lt;"))
    try:
        r = subprocess.run([EXE, "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=25,
                           creationflags=0x08000000)
        if r.returncode != 0:
            log(f"通知失败 rc={r.returncode} err={(r.stderr or b'').decode('utf-8', 'replace')[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log(f"通知异常：{type(e).__name__}: {e}")
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"down_since": {}, "notified": {}, "up_notified": {}}


def save_state(st: dict) -> None:
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"状态写入失败：{e}")


def body_down(p: int, down_min: float) -> str:
    who = "机器人号" if p == 3001 else "你自己的号"
    return (f"端口 {p}（{who}）已经断 {down_min:.0f} 分钟，还是连不上。"
            "多半是 QQ 掉登录态、退回扫码界面——去 NapCat 控制台扫码就能恢复。")


def body_up(p: int, down_min: float) -> str:
    who = "机器人号" if p == 3001 else "你自己的号"
    return f"端口 {p}（{who}）已恢复，刚才断了 {down_min:.0f} 分钟。"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default=",".join(str(p) for p in DEFAULT_PORTS))
    ap.add_argument("--minutes", type=float, default=5.0, help="连续多少分钟不通才通知")
    ap.add_argument("--interval", type=float, default=30.0, help="探测间隔（秒）")
    ap.add_argument("--repeat-minutes", type=float, default=30.0,
                    help="断线持续期间，每隔多少分钟重弹一次（生产 30）")
    ap.add_argument("--once", action="store_true", help="只探一次")
    ap.add_argument("--dry", action="store_true", help="只打印要发的通知，不真弹")
    args = ap.parse_args()

    ports = [int(x) for x in args.ports.split(",") if x.strip()]
    st = load_state()
    st.setdefault("down_since", {})
    st.setdefault("notified", {})
    st.setdefault("up_notified", {})
    st.setdefault("last_notified_at", {})

    if args.once:
        for p in ports:
            ok = probe(p)
            log(f"探活 {p}: {'ON' if ok else 'OFF'}")
            if not args.dry:
                continue
            key = str(p)
            since = st["down_since"].get(key)
            if ok:
                if since:
                    log(f"[dry] 恢复通知：{body_up(p, (time.time() - since) / 60)}")
            else:
                down_min = (time.time() - since) / 60 if since else 0.0
                log(f"[dry] 掉线通知：{body_down(p, down_min)}")
        return 0

    need = args.minutes * 60
    repeat = max(1.0, args.repeat_minutes * 60)
    log(f"看门狗启动：端口 {ports}，间隔 {args.interval:.0f}s，阈值 {args.minutes:g} 分钟，"
        f"断线期间每 {args.repeat_minutes:g} 分钟重弹一次")

    while True:
        for p in ports:
            key = str(p)
            ok = probe(p)
            now = time.time()
            if ok:
                if st["down_since"].get(key):
                    gap = now - st["down_since"][key]
                    log(f"端口 {p} 恢复（断了 {gap / 60:.1f} 分钟）")
                    if not st["up_notified"].get(key):
                        body = body_up(p, gap / 60)
                        if args.dry:
                            log(f"[dry] 恢复通知：{body}")
                            st["up_notified"][key] = True
                        elif toast("QQ 已恢复", body):
                            log("已弹通知（恢复）")
                            st["up_notified"][key] = True
                st["down_since"].pop(key, None)
                st["notified"].pop(key, None)
                st["last_notified_at"].pop(key, None)
            else:
                st["down_since"].setdefault(key, now)
                down = now - st["down_since"][key]
                last = st["last_notified_at"].get(key) or 0.0
                if down >= need and (not st["notified"].get(key) or now - last >= repeat):
                    body = body_down(p, down / 60)
                    log(f"告警：端口 {p} 已断 {down / 60:.0f} 分钟")
                    if args.dry:
                        log(f"[dry] 通知：QQ 掉线了 / {body}")
                        st["notified"][key] = True
                        st["last_notified_at"][key] = now
                        st["up_notified"][key] = False
                    elif toast("QQ 掉线了", body):
                        log("已弹通知")
                        st["notified"][key] = True
                        st["last_notified_at"][key] = now
                        st["up_notified"][key] = False
        save_state(st)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
