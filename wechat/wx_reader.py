# -*- coding: utf-8 -*-
"""微信只读采集器（UIA 不可用，走 PrintWindow + Windows OCR）。

用法：
    python wx_reader.py --once        抓一次，有新内容才落盘
    python wx_reader.py --loop        每 20 秒抓一次
    python wx_reader.py --dump        抓一次并打印识别到的行（调试）

产物：
    C:\\Users\\yourname\\.dsh\\wechat-read\\YYYY-MM-DD.jsonl   每行一条 {ts, title, lines:[...], text}
    C:\\Users\\yourname\\.dsh\\wechat-read\\last_text.txt     最近一次的完整文本（便于直接看）
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image

HOME = Path(r"C:\Users\yourname\.dsh\wechat-read")
HOME.mkdir(parents=True, exist_ok=True)
STATE = HOME / "state.json"
LAST_TEXT = HOME / "last_text.txt"
SHOT = HOME / "last_shot.png"
FOCUS = HOME / "focus.json"

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDPIAware()

POLL = 20.0

# 上一次抓到的像素指纹（同一屏不重复跑 OCR）
_LAST_IMG: dict = {}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    try:
        with open(HOME / "wx_reader.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def find_wechat():
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if buf.value == "微信" and cls.value.startswith("Qt51514"):
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append({
                "hwnd": hwnd, "title": buf.value,
                "iconic": bool(user32.IsIconic(hwnd)),
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
            })
        return True

    user32.EnumWindows(cb, 0)
    return found


def print_window(hwnd, w, h, flags=2):
    hwndDC = user32.GetWindowDC(hwnd)
    mfcDC = gdi32.CreateCompatibleDC(hwndDC)
    bmp = gdi32.CreateCompatibleBitmap(hwndDC, w, h)
    gdi32.SelectObject(mfcDC, bmp)
    ok = user32.PrintWindow(hwnd, mfcDC, flags)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mfcDC, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfcDC)
    user32.ReleaseDC(hwnd, hwndDC)
    return ok, img


async def ocr_file(path: str):
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        return None
    sf = await StorageFile.get_file_from_path_async(path)
    stream = await sf.open_read_async()
    decoder = await BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bmp)
    out = []
    for line in result.lines:
        if not line.words:
            continue
        r = line.words[0].bounding_rect
        out.append((int(r.y), int(r.x), "".join(w.text for w in line.words)))
    out.sort()
    return out


def capture_once():
    wins = find_wechat()
    if not wins:
        return None, "没找到微信窗口"
    w0 = wins[0]
    hwnd = w0["hwnd"]
    _, _, w, h = w0["rect"]
    if w <= 0 or h <= 0:
        return None, "窗口尺寸异常"
    if w0["iconic"]:
        return None, "微信最小化中，抓不到画面"
    ok, img = print_window(hwnd, w, h)
    if not ok:
        return None, "PrintWindow 失败"

    # 只留会话面板（左边是会话列表，OCR 出来全是噪声）
    pane_x = int(w * 0.16)
    img = img.crop((pane_x, 0, w, h))

    # 先比像素：这一屏没变就不用跑 OCR（省 CPU）
    raw_fp = hashlib.sha1(img.tobytes()).hexdigest()
    if raw_fp == _LAST_IMG.get("fp") and not os.environ.get("WX_FORCE_OCR"):
        return {"win": w0, "img": img, "lines": None, "raw_fp": raw_fp}, None
    _LAST_IMG["fp"] = raw_fp

    # 放大 2 倍再识别：实测「示例分组B/口语/加油」这类词能救回来
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img.save(SHOT)

    lines = asyncio.run(ocr_file(str(SHOT)))
    if lines is None:
        return None, "没有 OCR 语言包"
    return {"win": w0, "img": img, "lines": lines}, None


def load_focus() -> dict:
    """重点群白名单。每次轮询都重读，改 json 即时生效。"""
    default = {"enabled": False, "groups": []}
    if not FOCUS.exists():
        return default
    try:
        cfg = json.loads(FOCUS.read_text(encoding="utf-8"))
    except Exception as e:
        log("focus.json 读不了：%s" % e)
        return default
    if not isinstance(cfg, dict):
        return default
    return {
        "enabled": bool(cfg.get("enabled")),
        "groups": [str(g) for g in (cfg.get("groups") or []) if str(g).strip()],
        "activePoll": bool(cfg.get("activePoll")),
    }


def norm(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def hit_focus(title: str, cfg: dict) -> bool:
    if not cfg.get("enabled"):
        return True
    if not cfg.get("groups"):
        return False
    t = norm(title)
    if not t:
        return False
    return any(norm(g) and norm(g) in t for g in cfg["groups"])


def split_title(lines, w, h):
    """顶部那行大字就是当前会话名（crop 后 x 已经很小，别再按 x 过滤）。"""
    cands = [t.strip() for y, x, t in lines if y < h * 0.09 and len(t.strip()) > 1]
    for t in cands:
        if t not in ("0", "O", "o") and not t.isdigit():
            return t
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--interval", type=float, default=POLL)
    args = ap.parse_args()

    if not (args.once or args.loop or args.dump):
        args.once = True

    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    while True:
        t0 = time.time()
        data, err = capture_once()
        if err:
            log("跳过：" + err)
            if args.dump:
                print("ERR:", err)
            if not args.loop:
                return
        else:
            if data.get("lines") is None:
                if args.dump:
                    print("这一屏没变（像素指纹相同），跳过 OCR")
                    return
                if not args.loop:
                    return
                time.sleep(max(1.0, args.interval - (time.time() - t0)))
                continue
            w = data["img"].width
            h = data["img"].height
            title = split_title(data["lines"], w, h)
            keep = [(y, x, t) for y, x, t in data["lines"]]
            body = "\n".join(t for _, _, t in keep)
            fp = hashlib.sha1(body.encode("utf-8")).hexdigest()

            if args.dump:
                print("窗口: %s  %dx%d  会话: %r" % (data["win"]["title"], w, h, title))
                print("行数: %d（会话栏保留 %d）" % (len(data["lines"]), len(keep)))
                print("-" * 50)
                for y, x, t in keep:
                    print("  y=%4d x=%4d | %s" % (y, x, t))
                return

            if fp != state.get("fp"):
                cfg = load_focus()
                if not hit_focus(title, cfg):
                    state["fp"] = fp
                    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                    log("不在重点群，跳过：%r" % title)
                    if not args.loop:
                        return
                    time.sleep(max(1.0, args.interval - (time.time() - t0)))
                    continue
                rec = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "title": title,
                    "lines": [{"y": y, "x": x, "t": t} for y, x, t in keep],
                    "text": body,
                }
                day = time.strftime("%Y-%m-%d")
                with open(HOME / (day + ".jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                LAST_TEXT.write_text("【%s】%s\n\n%s" % (rec["ts"], title, body), encoding="utf-8")
                state["fp"] = fp
                STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                log("新增一屏：%r（%d 行）" % (title, len(keep)))
            else:
                log("无变化（%r）" % title)

        if not args.loop:
            return
        spent = time.time() - t0
        time.sleep(max(1.0, args.interval - spent))


if __name__ == "__main__":
    main()
