"""R5-5 shadow 对比：img_ingest 拆分前后（v2 分派器 vs legacy）必须 100% 一致。

跑法：& python shadow_img_test.py
  - Q_SELFTEST=1，路径全挪临时目录，不写生产 trace/shadow
  - send 全截获，不真发
用例：单图片 / 双图片 / 图片+文件（复用 R20 输入）/ R19 fetch 链四子（A/B/C1/C2）
对比项：payload_sha1、img_desc_sha1、usecase_returns["img"]、图片段在 payload 里的起点
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["Q_SELFTEST"] = "1"

import qq_watch as Q          # noqa: E402
import usecase as UC          # noqa: E402
import regress as R           # noqa: E402

OWNER = 100000001
IMG1 = "一张蓝色海洋风格的图表截图，标题写着 vector space"
IMG2 = "第二张图，手写的线性代数笔记，能看到矩阵和箭头"
FAKE_IMG = "C:/fake/pic.jpg"


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16]


def fresh_tmp(tag: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"shadowimg-{tag}-"))
    R.redirect_paths(p)
    Q.SHADOW_FILE = p / "shadow.jsonl"
    return p


def last_shadow() -> dict:
    p = Q.SHADOW_FILE
    if not p.exists():
        return {}
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows[-1] if rows else {}


def img_seg(name: str = "a.jpg") -> dict:
    return {"type": "image", "data": {"file": name, "url": f"https://fake/{name}"}}


async def run_case(label: str, segs, text: str, fetch_file=None,
                   call_impl=None, find_impl=None, expect_imgs: int = 0) -> dict:
    tmp = fresh_tmp(label)
    got = {}
    descs = {1: [IMG1], 2: [IMG1, IMG2]}

    def fake_ask(payload, nick, uid=None, is_owner=True, session_id=""):
        got["payload"] = payload
        return ("收到。", "none")

    async def fake_fetch_image(self, d):
        return Path(FAKE_IMG)

    def fake_describe(p):
        # 单图/多图分别给不同描述；这里按调用序号发放
        n = got.get("_dc", 0) + 1
        got["_dc"] = n
        seq = descs[expect_imgs] if expect_imgs in descs else [IMG1] * max(expect_imgs, 1)
        return seq[n - 1] if n <= len(seq) else ""

    orig = (Q.ask_me, Q.Bot.fetch_image, Q.describe_image, Q.Bot.fetch_file,
            Q.Bot.call, Q._find_received_file)
    Q.ask_me = fake_ask
    Q.Bot.fetch_image = fake_fetch_image
    Q.describe_image = fake_describe
    # 注意：R19 要跑「真 fetch_file → 假 call」的降级链，所以只有显式给了 fetch_file 才换
    if fetch_file is not None:
        Q.Bot.fetch_file = fetch_file
    if call_impl:
        Q.Bot.call = call_impl
    if find_impl:
        Q._find_received_file = find_impl
    rec_ = R.Recorder().install()
    try:
        bot = R.make_bot()
        bot.cfg["usecaseMode"] = "shadow"
        await bot._handle_one(R.ev(OWNER, text=text, mid=f"shadow-{label}", segs=segs))
    finally:
        rec_.uninstall()
        (Q.ask_me, Q.Bot.fetch_image, Q.describe_image, Q.Bot.fetch_file,
         Q.Bot.call, Q._find_received_file) = orig

    payload = got.get("payload") or ""
    rec = last_shadow()
    exp_desc = "；".join(descs.get(expect_imgs, []))[:Q.IMG_DESC_CHARS] if expect_imgs else ""
    row = {
        "case": label,
        "payload_sha1": rec.get("payload_sha1"),
        "payload_ok": rec.get("payload_sha1") == sha1(payload),
        "img_desc_sha1": rec.get("img_desc_sha1"),
        "img_desc_ok": rec.get("img_desc_sha1") == sha1(exp_desc),
        "img_chars": rec.get("img_desc_chars"),
        "img_at": rec.get("img_at"),
        "img_at_ok": (rec.get("img_at") == (payload.find(exp_desc) if exp_desc else -1)),
        "ret_img": (rec.get("usecase_returns") or {}).get("img"),
        "payload_chars": len(payload),
    }
    row["same"] = bool(row["payload_ok"] and row["img_desc_ok"] and row["img_at_ok"])
    shutil.rmtree(tmp, ignore_errors=True)
    return row


async def main() -> int:
    rows = []

    # 1) 单图片
    rows.append(await run_case("单图片", [img_seg("one.jpg")], "看看这张", expect_imgs=1))

    # 2) 双图片
    rows.append(await run_case("双图片", [img_seg("a.jpg"), img_seg("b.jpg")], "两张都看看",
                               expect_imgs=2))

    # 3) 图片 + 文件（复用 R20：文件段在图片段之后）
    tmp3 = Path(tempfile.mkdtemp(prefix="shadowimg-f-"))
    note = tmp3 / "note.txt"
    note.write_text("R20-REGRESS 示例私事A 向量空间 知识点", encoding="utf-8")

    async def fetch_note(self, d):
        return note

    rows.append(await run_case("图片+文件", [img_seg("a.jpg"),
                                            {"type": "file", "data": {"file": "note.txt"}}],
                               "图和文件都给你", fetch_file=fetch_note, expect_imgs=1))

    # 4) R19 fetch 链四子（图片不参与，只验 img 段为空、payload 一致）
    async def call_ok(self, action, params, timeout=45):
        return {"status": "ok", "data": {"file": str(note)}}

    async def call_pp_fail(self, action, params, timeout=45):
        if action == "get_private_file_url":
            raise RuntimeError("packetBackend不可用")
        if action == "get_group_file_url":
            return {"data": {"url": str(note)}}
        raise RuntimeError("不该走到 " + action)

    async def call_all_fail(self, action, params, timeout=45):
        raise RuntimeError("boom")

    # _find_received_file 是同步函数（fetch_file 里走 asyncio.to_thread），不能写成 async
    def find_hit(name, minutes=15):
        return note

    def find_miss(name, minutes=15):
        return None

    async def fetch_none(self, d):
        return None

    fsegs = [{"type": "file", "data": {"file_id": "F19", "file": "note.txt"}}]
    rows.append(await run_case("R19-A", fsegs, "看看文件",
                               call_impl=call_ok, find_impl=find_miss, expect_imgs=0))
    rows.append(await run_case("R19-B", fsegs, "看看文件",
                               call_impl=call_pp_fail, find_impl=find_miss, expect_imgs=0))
    rows.append(await run_case("R19-C1", fsegs, "看看文件",
                               call_impl=call_all_fail, find_impl=find_hit, expect_imgs=0))
    rows.append(await run_case("R19-C2", fsegs, "看看文件",
                               call_impl=call_all_fail, find_impl=find_miss, expect_imgs=0))

    print("| case | payload_sha1 | payload=legacy | img_desc_sha1 | desc=legacy | img_at | img_at ok | returns[img] | same |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['case']} | `{r['payload_sha1']}` | {r['payload_ok']} | "
              f"`{r['img_desc_sha1']}` | {r['img_desc_ok']} | {r['img_at']} | {r['img_at_ok']} | "
              f"{r['ret_img']} | **{r['same']}** |")
    bad = [r["case"] for r in rows if not r["same"]]
    print()
    print(f"一致 {len(rows) - len(bad)}/{len(rows)}" + (f"  不一致：{bad}" if bad else ""))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
