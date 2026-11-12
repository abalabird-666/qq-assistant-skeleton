# -*- coding: utf-8 -*-
"""R5 第三段：shadow 骨架验证（不改业务、不真发、不动生产 trace）。

做四件事：
1. 记录生产 `trace.jsonl` / `trace_shadow.jsonl` 的字节数（跑完要一字节不差）
2. 把 usecaseMode 切成 shadow，喂 3 条事件（纯文本 / 文件+文本 / 微信月报指令）
3. 断言：shadow 记录 3 条、payload_sha1 都有、reply_skipped 全 true、
        ask_me 一次都没被调（把 ask_me 换成「一调就炸」的桩）
4. 输出 same_as_legacy 统计 + 生产 trace 前后对比

用法：python shadow_test.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["Q_SELFTEST"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qq_watch as Q  # noqa: E402

PROD_TRACE = HERE / "trace.jsonl"
PROD_SHADOW = HERE / "trace_shadow.jsonl"


def size(p: Path) -> int:
    try:
        return p.stat().st_size
    except Exception:
        return -1


def rows(p: Path) -> list[dict]:
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def ev(uid, text="", mid="", segs=None, nick=""):
    return {
        "post_type": "message", "message_type": "private", "user_id": uid,
        "message_id": mid or str(int(time.time() * 1000)),
        "sender": {"nickname": nick or str(uid)},
        "message": segs if segs is not None else [{"type": "text", "data": {"text": text}}],
    }


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r5-shadow-"))
    before = (size(PROD_TRACE), size(PROD_SHADOW))
    print(f"生产 trace.jsonl   = {before[0]} B")
    print(f"生产 trace_shadow  = {before[1]} B")
    print(f"临时目录           = {tmp}\n")

    # 全部写盘路径挪到临时目录
    Q.STATE = tmp / "state.json"
    Q.TRACE_FILE = tmp / "trace.jsonl"
    Q.SHADOW_FILE = tmp / "trace_shadow.jsonl"
    Q.DOCS_DB = tmp / "docs.json"
    Q.LAST_DOC = tmp / "last_doc.json"
    Q.FILES_DIR = tmp / "files"
    Q.OUT_DIR = tmp / "out"
    Q.FILES_DIR.mkdir(parents=True, exist_ok=True)
    Q.OUT_DIR.mkdir(parents=True, exist_ok=True)

    cfg = Q.load_config()
    cfg["usecaseMode"] = "shadow"
    bot = Q.Bot(cfg)
    bot.state = {"seen": [], "day": "", "n": 0, "last_sent": {}}

    asked = []

    def boom(*a, **k):
        asked.append(a)
        raise AssertionError("shadow 模式下不许调 ask_me！")

    orig_ask = Q.ask_me
    Q.ask_me = boom

    async def fake_fetch_file(self, d):
        p = tmp / "f.txt"
        p.write_text("附件正文", encoding="utf-8")
        return p

    orig_fetch = Q.Bot.fetch_file
    Q.Bot.fetch_file = fake_fetch_file
    orig_digest = Q.wx_digest
    Q.wx_digest = lambda days=30: "【示例分组B群】11-16 随堂"
    try:
        t0 = time.time()
        await bot._handle_one(ev(100000001, "明天几点上课", mid="shadow-a"))
        await bot._handle_one(ev(100000001, mid="shadow-b",
                                 segs=[{"type": "file", "data": {"file": "f.txt"}},
                                       {"type": "text", "data": {"text": "顺便看一下"}}]))
        await bot._handle_one(ev(100000001, "帮我汇总一下这个月的微信", mid="shadow-c"))
        cost = time.time() - t0
    finally:
        Q.ask_me = orig_ask
        Q.Bot.fetch_file = orig_fetch
        Q.wx_digest = orig_digest

    recs = rows(Q.SHADOW_FILE)
    print(f"shadow 记录 {len(recs)} 条（期望 3），耗时 {cost:.2f}s")
    for r in recs:
        ints = ",".join(i["kind"] for i in r.get("intents") or []) or "（无）"
        print(f"  {r['mid']:9s} mode={r['mode']} sha1={r['payload_sha1']} "
              f"chars={r['payload_chars']:5d} reply_skipped={r['reply_skipped']} "
              f"same={r['same_as_legacy']} intents=[{ints}]")

    ok = True
    if len(recs) != 3:
        ok = False
        print("  ✗ 记录条数不对")
    if asked:
        ok = False
        print(f"  ✗ ask_me 被调了 {len(asked)} 次（shadow 下应为 0）")
    if any(not r.get("payload_sha1") for r in recs):
        ok = False
        print("  ✗ 有记录缺 payload_sha1")
    if any(r.get("reply_skipped") is not True for r in recs):
        ok = False
        print("  ✗ 有记录 reply_skipped != true")
    same = [r.get("same_as_legacy") for r in recs]
    rate = (sum(1 for x in same if x) / len(same) * 100) if same else 0
    print(f"\nsame_as_legacy = {rate:.0f}%（{sum(1 for x in same if x)}/{len(same)}）")

    after = (size(PROD_TRACE), size(PROD_SHADOW))
    print(f"生产 trace.jsonl   跑后 = {after[0]} B（前 {before[0]}）→ "
          f"{'未变 ✅' if after[0] == before[0] else '变了 ❌'}")
    print(f"生产 trace_shadow  跑后 = {after[1]} B（前 {before[1]}）→ "
          f"{'未变 ✅' if after[1] == before[1] else '变了 ❌'}")
    if after[0] != before[0] or after[1] != before[1]:
        ok = False

    # 别忘了把开关写回 legacy（这个脚本只改内存里的 cfg，不改文件）
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n结果：" + ("✅ PASS" if ok else "❌ FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
