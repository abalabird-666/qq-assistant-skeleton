# -*- coding: utf-8 -*-
"""按群名去微信里「翻」一屏：点开会话 → 截图 → OCR → 输出文字。

用法：
    python wx_fetch.py --list                 列出左侧会话名（OCR）
    python wx_fetch.py --group 示例分组B           点开含「示例分组B」的会话并读一屏
    python wx_fetch.py --group 示例分组B --json    同上，输出 JSON

只做两件事：点一下会话列表、截一张图。不发消息、不改数据。
点完会把鼠标放回原位；微信窗口保持在你原来打开的那个群（会留在新群上）。
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import wx_reader as R  # noqa: E402

user32 = ctypes.windll.user32


def list_lines(img: Image.Image):
    """OCR 左侧会话列表，返回 [(y_orig, x_orig, text)]。"""
    w, h = img.size
    left = img.crop((0, 0, int(w * 0.17), h))
    p = str(R.HOME / "t_left.png")
    left = left.resize((left.width * 2, left.height * 2), Image.LANCZOS)
    left.save(p)
    lines = asyncio.run(R.ocr_file(p)) or []
    return [(y // 2, x // 2, t) for y, x, t in lines]


def click(x: int, y: int):
    """真实点击，点完把光标放回原处。"""
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # down
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # up
    time.sleep(0.15)
    user32.SetCursorPos(pt.x, pt.y)


def ensure_visible(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--wait", type=float, default=1.4, help="点开后等多久再截图")
    args = ap.parse_args()

    wins = R.find_wechat()
    if not wins:
        print(json.dumps({"ok": False, "err": "没找到微信窗口"}, ensure_ascii=False))
        return 1
    w0 = wins[0]
    hwnd = w0["hwnd"]
    ensure_visible(hwnd)
    _, _, W, H = w0["rect"]

    ok, img = R.print_window(hwnd, W, H)
    if not ok:
        print(json.dumps({"ok": False, "err": "PrintWindow 失败"}, ensure_ascii=False))
        return 1

    rows = list_lines(img)

    if args.list:
        names = [t.strip() for _, _, t in rows if len(t.strip()) >= 2]
        print("\n".join(names))
        return 0

    key = args.group.strip()
    if not key:
        print(json.dumps({"ok": False, "err": "要指定 --group"}, ensure_ascii=False))
        return 1

    # 找最靠上的一条命中（会话列表里靠上的一般就是最近聊过的）
    hit = None
    for y, x, t in rows:
        if key in t:
            hit = (y, x, t)
            break
    if not hit and len(key) >= 2:
        # 退一步：去掉末字再匹配（OCR 常把最后一个字认错）
        for y, x, t in rows:
            if key[:-1] in t:
                hit = (y, x, t)
                break
    if not hit:
        print(json.dumps({"ok": False, "err": "会话列表里没找到「%s」" % key}, ensure_ascii=False))
        return 2

    y, x, name = hit
    click(int(W * 0.09), y + 6)
    time.sleep(args.wait)

    ok, img2 = R.print_window(hwnd, W, H)
    if not ok:
        print(json.dumps({"ok": False, "err": "点击后截图失败"}, ensure_ascii=False))
        return 1

    pane = img2.crop((int(W * 0.16), 0, W, H))
    shot = R.HOME / "last_fetch.png"
    pane = pane.resize((pane.width * 2, pane.height * 2), Image.LANCZOS)
    pane.save(shot)
    lines = asyncio.run(R.ocr_file(str(shot))) or []
    title = R.split_title(lines, pane.width, pane.height)
    text = "\n".join(t for _, _, t in lines)

    rec = {
        "ok": True,
        "asked": key,
        "matched_row": name,
        "title": title,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "lines": [{"y": y2, "x": x2, "t": t} for y2, x2, t in lines],
        "text": text,
        "shot": str(shot),
    }

    # 存档也落一份
    day = time.strftime("%Y-%m-%d")
    with open(R.HOME / (day + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": rec["ts"], "title": title, "lines": rec["lines"],
                            "text": text, "src": "fetch"}, ensure_ascii=False) + "\n")

    if args.json:
        out = {k: rec[k] for k in ("ok", "asked", "matched_row", "title", "ts", "text")}
        print(json.dumps(out, ensure_ascii=False))
    else:
        print("会话：%s（点的是「%s」）" % (title, name))
        print("-" * 40)
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
