# -*- coding: utf-8 -*-
"""大肥鱼表情包库 —— 记账 + 取用。

规矩：
1. 只收「大肥鱼/鱼」形象的图（自己画的、或从网上扒的鱼系梗图）；
2. 每张都登记用途，取用时按 tag 挑，不瞎发；
3. 不刷屏：一轮对话最多配一张。

用法：
    python sticker_lib.py list
    python sticker_lib.py pick 干饭
    python sticker_lib.py cq 干饭         # 直接给 NapCat 的 CQ 码
    python sticker_lib.py add <文件> <标签,标签> [用途说明]
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent
LIB = HOME / "stickers"
INDEX = HOME / "stickers.json"


def load() -> list[dict]:
    if not INDEX.exists():
        return []
    return json.loads(INDEX.read_text(encoding="utf-8"))


def save(items: list[dict]) -> None:
    INDEX.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_list() -> int:
    items = load()
    if not items:
        print("库是空的")
        return 0
    for it in items:
        print(f"{it['file']:<28} tags={','.join(it.get('tags', []))}  用途={it.get('use', '')}")
    print(f"\n共 {len(items)} 张，目录 {LIB}")
    return 0


def cmd_pick(tag: str) -> int:
    items = [it for it in load() if tag in it.get("tags", [])] or load()
    if not items:
        print("")
        return 1
    path = LIB / random.choice(items)["file"]
    print(str(path) if path.exists() else "")
    return 0 if path.exists() else 1


def cmd_path(tag: str) -> int:
    """只给图片路径（发给 NapCat 的「图片段」时用这个）。"""
    items = [it for it in load() if tag in it.get("tags", [])] or load()
    if not items:
        return 1
    path = LIB / random.choice(items)["file"]
    print(path.as_posix() if path.exists() else "")
    return 0 if path.exists() else 1


def cmd_cq(tag: str) -> int:
    items = [it for it in load() if tag in it.get("tags", [])] or load()
    if not items:
        print("")
        return 1
    name = random.choice(items)["file"]
    path = LIB / name
    if not path.exists():
        print("")
        return 1
    print(f"[CQ:image,file=file:///{path.as_posix()}]")
    return 0


def cmd_add(src: str, tags: str, use: str = "") -> int:
    src_path = Path(src)
    if not src_path.exists():
        print(f"文件不存在：{src}")
        return 1
    LIB.mkdir(parents=True, exist_ok=True)
    dst = LIB / src_path.name
    if dst.resolve() != src_path.resolve():
        shutil.copy2(src_path, dst)
    items = load()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    items = [it for it in items if it["file"] != dst.name]
    items.append({"file": dst.name, "tags": tag_list, "use": use})
    save(items)
    print(f"已收进库：{dst.name}  tags={tag_list}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 0
    op = argv[1]
    if op == "list":
        return cmd_list()
    if op == "pick" and len(argv) > 2:
        return cmd_pick(argv[2])
    if op == "path" and len(argv) > 2:
        return cmd_path(argv[2])
    if op == "cq" and len(argv) > 2:
        return cmd_cq(argv[2])
    if op == "add" and len(argv) > 3:
        return cmd_add(argv[2], argv[3], argv[4] if len(argv) > 4 else "")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
