"""R9（session 按 uid 隔离）+ B-07（无标记兜底）自测。

不连 NapCat、不调模型：只把 subprocess.run 换成假的，其余全走真实代码路径。
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["Q_SELFTEST"] = "1"          # R9.5-3：日志/trace 不许写进生产文件
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qq_watch as Q

MASTER = 100000001
CR = 100000002
LEGACY = "session-d6003f0b-acf2-4231-ba0d-daba7ba38edd"

tmp = Path(tempfile.mkdtemp(prefix="r9-"))
(tmp / "config.json").write_text(
    json.dumps({
        "masterUins": [MASTER], "replyUins": [CR], "token": "x",
        "sessionPerUid": True, "docScope": "owner-only", "llmTimeoutSec": 30,
    }, ensure_ascii=False), encoding="utf-8")
(tmp / "qq-session.json").write_text(json.dumps({"id": LEGACY}), encoding="utf-8")
sess = tmp / "sessions"
sess.mkdir()

# 把模块里的路径全部指到临时目录
Q.HOME = tmp
Q.CONFIG = tmp / "config.json"
Q.CFG = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
Q.SESSION_FILE = tmp / "qq-session.json"
Q.SESSIONS_DIR = sess
Q.TRACE_FILE = tmp / "trace.jsonl"
Q.MASTER_UINS = {MASTER}
Q.LLM_TIMEOUT = 30
Q.DSH_EXE = Path(sys.executable)      # 只用来过 exists() 检查

USED = []          # 每次假 subprocess 用了哪个 sid
MODE = {"no_marker": False}


def fake_run(args, **kw):
    """假 run：模拟 dsh 新建会话 + 吐标记。"""
    use_sid = ""
    if "--session-id" in args:
        use_sid = args[args.index("--session-id") + 1]
    if not use_sid:
        use_sid = f"session-new{len(USED)}-{int(time.time()*1000)%100000}"
        d = sess / use_sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "session.v4.jsonl.zstd").write_bytes(b"x")
        used = use_sid
    else:
        used = use_sid
    USED.append(used)
    if MODE["no_marker"]:
        out = "dsh: warning something\n这里是一段没有标记的正文，应该被兜底取走。\n"
    else:
        out = "<<<REPLY>>>pong<<<END>>>\n<<<S>>>none<<<E>>>\n"
    return subprocess.CompletedProcess(args, 0, out.encode("utf-8"), b"")


Q.subprocess.run = fake_run

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")


print(f"临时目录：{tmp}")

print("\nT1 主人首聊 → 认领旧单例 d6003f0b")
USED.clear()
r1 = Q.ask_me("主人测试一句话", "主人", MASTER, True)
f1 = tmp / f"qq-session-{MASTER}.json"
check("生成主人文件", f1.exists(), str(f1.name))
check("id == 旧单例", f1.exists() and json.loads(f1.read_text(encoding="utf-8"))["id"] == LEGACY)
check("本次用的是旧 id", USED == [LEGACY], f"used={USED}")
check("拿到回复", r1[0] == "pong", f"reply={r1[0]!r}")

print("\nT2 白名单 uid 首聊 → 独立新会话，不含主人记录")
USED.clear()
r2 = Q.ask_me("CR 测试一句话", "CR", CR, False)
f2 = tmp / f"qq-session-{CR}.json"
check("生成 CR 文件", f2.exists(), str(f2.name))
sid2 = json.loads(f2.read_text(encoding="utf-8"))["id"] if f2.exists() else ""
check("id != 旧单例", sid2 and sid2 != LEGACY, f"cr={sid2}")
check("本次用的是新 id", USED == [sid2], f"used={USED}")
check("两份文件 id 不同", sid2 != LEGACY)
# 关键：CR 的会话是全新的空会话，里面不能出现主人的话
q1 = tmp / f"qq-session-{MASTER}.json"
check("主人文件仍是旧 id（没被 CR 覆盖）",
      json.loads(q1.read_text(encoding="utf-8"))["id"] == LEGACY)

print("\nT3 关开关 → 回落旧单例，行为不变")
Q.CFG["sessionPerUid"] = False
f2_before = f2.read_text(encoding="utf-8")
USED.clear()
r3 = Q.ask_me("关开关后 CR 再说话", "CR", CR, False)
check("回落用旧 id", USED == [LEGACY], f"used={USED}")
check("CR 文件没被改写", f2.read_text(encoding="utf-8") == f2_before)
check("主人文件也没被改", json.loads(q1.read_text(encoding="utf-8"))["id"] == LEGACY)
Q.CFG["sessionPerUid"] = True

print("\nT4 B-07：模型不吐标记 → 兜底取最后一段，不丢消息")
MODE["no_marker"] = True
r4 = Q.ask_me("触发无标记", "CR", CR, False)
MODE["no_marker"] = False
check("兜底拿到正文", bool(r4[0]) and "没有标记的正文" in r4[0], f"reply={r4[0]!r}")
check("没有混进 dsh 噪声", "dsh:" not in r4[0])
tr = [json.loads(x) for x in Q.TRACE_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
codes = [t.get("code") for t in tr]
check("trace 记了 NO_MARKER_FALLBACK", "NO_MARKER_FALLBACK" in codes, f"codes={codes[-3:]}")

print("\nT5 sessionPerUid=false 时 read_sid 的兼容性")
check("read_sid(None) = 旧 id", Q.read_sid(None) == LEGACY)
Q.CFG["sessionPerUid"] = True
check("read_sid(CR) = 自己的 id", Q.read_sid(CR) == sid2)

print("\n" + ("全部通过 ✅" if ok_all else "有失败 ❌"))
print("临时目录保留供检查：", tmp)
sys.exit(0 if ok_all else 1)
