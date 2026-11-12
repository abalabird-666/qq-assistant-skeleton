"""R9.5-1：真并发首聊（asyncio.gather + to_thread，不是顺序模拟）。

两个 uid 同时「第一次说话」，各自要绑到**自己这条**回复产生的 session。
用假 subprocess.run 模拟 dsh 新建会话：从 prompt 里认出这一轮属于谁（MARK-x），
新建的目录名带上这个标记，跑完直接看绑定的 id 里有没有对家的标记——有就是串了。
"""
import asyncio
import json
import os
import random
import sys
import tempfile
import time
import uuid
from pathlib import Path

os.environ["Q_SELFTEST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qq_watch as Q

MASTER = 100000001
CR = 100000002
SELF = 100000000
LEGACY = "session-d6003f0b-acf2-4231-ba0d-daba7ba38edd"

tmp = Path(tempfile.mkdtemp(prefix="r9c-"))
(tmp / "config.json").write_text(json.dumps({
    "masterUins": [MASTER], "replyUins": [CR, SELF], "token": "x",
    "sessionPerUid": True, "llmTimeoutSec": 60,
}, ensure_ascii=False), encoding="utf-8")
(tmp / "qq-session.json").write_text(json.dumps({"id": LEGACY}), encoding="utf-8")
sess = tmp / "sessions"
sess.mkdir()

Q.HOME = tmp
Q.CONFIG = tmp / "config.json"
Q.CFG = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
Q.SESSION_FILE = tmp / "qq-session.json"
Q.SESSIONS_DIR = sess
Q.TRACE_FILE = tmp / "trace_selftest.jsonl"
Q.LOG = tmp / "log.txt"
Q.MASTER_UINS = {MASTER}
Q.DSH_EXE = Path(sys.executable)
Q.LLM_TIMEOUT = 60


def fake_run(args, **kw):
    """假 dsh：睡一下（模拟生成耗时），认出是谁的 prompt，建一个带标记的会话目录。"""
    prompt = args[-1]
    mark = "UNKNOWN"
    for m in ("MARK-A", "MARK-B", "MARK-C"):
        if m in prompt:
            mark = m
            break
    time.sleep(random.uniform(0.05, 0.25))
    sid = f"session-{mark}-{uuid.uuid4().hex[:8]}"
    d = sess / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.v4.jsonl.zstd").write_bytes(b"x")
    return Q.subprocess.CompletedProcess(args, 0,
        f"<<<REPLY>>>{mark} 的回复<<<END>>>\n<<<S>>>none<<<E>>>\n".encode("utf-8"), b"")


Q.subprocess.run = fake_run


async def one(uid, mark):
    return await asyncio.to_thread(Q.ask_me, f"{mark} 首聊", f"u{uid}", uid, uid == MASTER)


async def round_of(pairs, label, rnd):
    for uid, _ in pairs:
        p = tmp / f"qq-session-{uid}.json"
        if p.exists():
            p.unlink()
    res = await asyncio.gather(*[one(uid, mark) for uid, mark in pairs])
    time.sleep(0.12)
    ok = True
    detail = []
    bound = {}
    for (uid, mark) in pairs:
        p = tmp / f"qq-session-{uid}.json"
        sid = json.loads(p.read_text(encoding="utf-8"))["id"] if p.exists() else ""
        bound[mark] = sid
        own = (sid == LEGACY) if uid == MASTER else (f"-{mark}-" in sid)
        detail.append(f"{mark}:{sid[:30]}")
        ok = ok and bool(sid) and own
    # 互不覆盖 + 不交叉
    vals = [v for v in bound.values() if v]
    no_cross = len(set(vals)) == len(vals)
    for (uid, mark) in pairs:
        sid = bound.get(mark, "")
        for other in ("A", "B", "C"):
            if f"MARK-{other}" != mark and f"-MARK-{other}-" in sid:
                ok = False
        if uid == MASTER and any(f"-MARK-{x}-" in sid for x in ("A", "B", "C")):
            ok = False      # 主人必须是认领的旧 id，不能是新会话
    ok = ok and no_cross
    if not ok and rnd == 1:
        print(f"    [debug] detail={detail} bound={bound} own_master_ok={bound.get('MARK-A','')==LEGACY} no_cross={no_cross}")
    print(f"  {label} 第{rnd}轮: {'PASS' if ok else 'FAIL'}   " + "  ".join(detail) +
          ("" if no_cross else "  ❌ id 重复(交叉)"))
    return ok


async def main():
    print("A 组：主人（认领旧 id）+ 白名单 CR（新建） 同时首聊")
    a_ok = 0
    for r in range(1, 6):
        a_ok += await round_of([(MASTER, "MARK-A"), (CR, "MARK-B")], "A", r)
    print(f"A 组结果: {a_ok}/5")

    print("\nB 组：两个非主人（CR + 机器人自号）同时首聊 —— 这才是真抢新 id 的场景")
    b_ok = 0
    for r in range(1, 6):
        # 主人文件每轮删掉，避免干扰
        b_ok += await round_of([(CR, "MARK-B"), (SELF, "MARK-C")], "B", r)
    print(f"B 组结果: {b_ok}/5")
    print(f"\n{tmp}")
    return a_ok, b_ok


a, b = asyncio.run(main())
print("\n" + ("并发首聊 10/10 全过 ✅" if a == 5 and b == 5 else f"有失败 ❌ A={a}/5 B={b}/5"))
sys.exit(0 if a == 5 and b == 5 else 1)
