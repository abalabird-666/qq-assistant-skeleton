"""R5-6 第二段验证：make_doc 的 2.1 幂等 / 2.2 undo（legacy 路径，进程内，不真发）。

跑法：& python md_guard_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["Q_SELFTEST"] = "1"

import qq_watch as Q          # noqa: E402
import regress as R           # noqa: E402

OWNER = 100000001
TEXT = "帮我做一份周末计划的word文档"


def make_fake_doc(n: int = 1, stem: str = ""):
    """假装生成一个文档。

    真 make_document 的名字是 `<标题>_<MMDD_HHMM>.docx`，同一分钟内同请求 → 同名；
    所以这里也按 stem 取名，才能真实复现「同 mid 同文件」的幂等场景。
    """
    p = Path(Q.OUT_DIR) / f"fake_doc_{stem or n}.docx"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"PK\x03\x04 fake docx")
    return p


async def case_dedup() -> tuple[bool, str]:
    tmp = Path(tempfile.mkdtemp(prefix="mdguard-dedup-"))
    R.redirect_paths(tmp)
    calls = []
    files = []

    def fake_make_document(request: str):
        import hashlib as _h
        stem = _h.sha1(request.encode("utf-8")).hexdigest()[:8]   # 同请求 → 同名
        files.append(make_fake_doc(len(files) + 1, stem))
        return files[-1]

    async def fake_send_file(self, user_id, path, **kw):
        calls.append(str(path))
        return True

    orig_make = Q.make_document
    rec = R.Recorder().install()
    orig_send = Q.Bot.send_file          # Recorder 装完之后再取，方便还原
    Q.make_document, Q.Bot.send_file = fake_make_document, fake_send_file
    try:
        bot = R.make_bot()
        for _ in range(2):                  # 同一条 mid 连打两次
            await bot._handle_one(R.ev(OWNER, text=TEXT, mid="md-dedup"))
            # 消息级去重（seen）会先挡掉第二条，这里清空 seen 才能测到「文件级」幂等
            bot.state["seen"] = []
        state = json.loads((Path(Q.STATE)).read_text(encoding="utf-8")) if Path(Q.STATE).exists() else {}
    finally:
        Q.Bot.send_file = orig_send
        Q.make_document = orig_make
        rec.uninstall()

    spans = [r for r in R.tr_rows() if r.get("tool") == "make_doc"]
    dup_rows = [s for s in spans if s.get("code") == "DEDUP"]
    left = sorted({str(p) for p in files if Path(p).exists()})   # 同名=同路径，去重再数
    sent_files = (state.get("sent_files") or {})
    ok = (len(calls) == 1 and len(files) == 2 and len(dup_rows) == 1
          and len(left) == 1 and len(sent_files) == 1)
    msg = (f"生成 2 份 / send_file 被调 {len(calls)} 次 / DEDUP span {len(dup_rows)} 条 / "
           f"实际落盘 {len(left)} 个（应 1，同名同路径，没有被误删）/ state.sent_files {len(sent_files)} 条")
    shutil.rmtree(tmp, ignore_errors=True)
    return ok, msg


async def case_undo() -> tuple[bool, str]:
    tmp = Path(tempfile.mkdtemp(prefix="mdguard-undo-"))
    R.redirect_paths(tmp)
    made = []

    def fake_make_document(request: str):
        made.append(make_fake_doc(len(made) + 1))
        return made[-1]

    async def fake_send_file(self, user_id, path, **kw):
        raise RuntimeError("模拟发送失败")

    orig_make = Q.make_document
    rec = R.Recorder().install()
    orig_send = Q.Bot.send_file          # Recorder 装完之后再取，方便还原
    Q.make_document, Q.Bot.send_file = fake_make_document, fake_send_file
    try:
        bot = R.make_bot()
        await bot._handle_one(R.ev(OWNER, text=TEXT, mid="md-undo"))
        state = json.loads((Path(Q.STATE)).read_text(encoding="utf-8")) if Path(Q.STATE).exists() else {}
    finally:
        Q.Bot.send_file = orig_send
        Q.make_document = orig_make
        rec.uninstall()

    left = [p for p in made if Path(p).exists()]
    sent_files = (state.get("sent_files") or {})
    ok = (len(made) == 1 and len(left) == 0 and len(sent_files) == 0)
    msg = (f"生成 1 份 / 失败后残留文件 {len(left)} 个（应 0，undo 已清）/ "
           f"state.sent_files {len(sent_files)} 条（应 0，没发出就不记）")
    shutil.rmtree(tmp, ignore_errors=True)
    return ok, msg


async def main() -> int:
    rows = []
    for name, fn in (("2.1 幂等", case_dedup), ("2.2 undo", case_undo)):
        t0 = time.time()
        ok, msg = await fn()
        rows.append((name, ok, msg, time.time() - t0))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} ({time.time() - t0:.1f}s)\n        {msg}")
    bad = [r[0] for r in rows if not r[1]]
    print()
    print(f"PASS {len(rows) - len(bad)} / FAIL {len(bad)}" + (f"  失败：{bad}" if bad else ""))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
