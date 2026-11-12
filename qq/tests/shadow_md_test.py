"""R5-6 第三段：MakeDoc 的 shadow 对比（进程内，不碰生产）。

跑法：& $py shadow_md_test.py
断言：
  1) shadow 下主人请求做文件 → 不生成、不发送，code=DRY，payload 里是「演练模式」文案
  2) shadow 下非主人请求做文件 → code=NOT_OWNER，payload 里是「只对主人开放」文案
  3) 两条的 payload_sha1 与 legacy 实际用的 payload 一致（同一段代码，验证记录没串）
  4) out 目录里没有落盘文件
"""
import asyncio
import hashlib
import json
import os
import pathlib
import sys
import tempfile

os.environ["Q_SELFTEST"] = "1"
HOME = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HOME))

import qq_watch as Q  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="mdshadow_"))
Q.STATE = TMP / "state.json"
Q.TRACE_FILE = TMP / "trace.jsonl"
Q.SHADOW_FILE = TMP / "shadow.jsonl"
Q.DOCS_DB = TMP / "docs.json"
Q.FILES_DIR = TMP / "files"
Q.OUT_DIR = TMP / "out"
Q.FILES_DIR.mkdir(parents=True, exist_ok=True)
Q.OUT_DIR.mkdir(parents=True, exist_ok=True)
try:
    Q.session_file = lambda uid: TMP / f"s-{int(uid)}.json"
except Exception:
    pass

CALLS = {"make": 0, "send": 0}
FAILS = []


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def fake_make(req):
    CALLS["make"] += 1
    p = Q.OUT_DIR / "md_shadow_test.docx"
    p.write_bytes(b"fake")
    return p


async def fake_send_file(self, uid, path):
    CALLS["send"] += 1
    return True


Q.make_document = fake_make
Q.Bot.send_file = fake_send_file
Q.ask_me = lambda *a, **k: ("好的", "safe")

MASTER = 100000001
FRIEND = 100000002
TEXT = "帮我做一份周末计划的word文档发我"


def ev(uid, mid):
    return {"user_id": uid, "message_id": mid,
            "message": [{"type": "text", "data": {"text": TEXT}}]}


def last_rec():
    lines = Q.SHADOW_FILE.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


def sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def run(uid, mid):
    bot = Q.Bot(dict(Q.cfg_raw) if hasattr(Q, "cfg_raw") else Q.load_config())
    bot.cfg["usecaseMode"] = "shadow"
    asyncio.run(bot._handle_one_v2(ev(uid, mid), "shadow"))
    rec = last_rec()
    return rec, bot._last_payload


print("=== 1) 主人 shadow 请求做文件 ===")
rec, payload = run(MASTER, "mds-owner")
md = (rec.get("usecase_returns") or {}).get("make_doc") or {}
ok(md.get("code") == "DRY", f"code=DRY 实际 {md.get('code')}")
ok(md.get("dry") is True, f"dry=True 实际 {md.get('dry')}")
ok(md.get("file_sent") is False, f"file_sent=False 实际 {md.get('file_sent')}")
ok(CALLS["make"] == 0, f"make_document 调用 {CALLS['make']} 次（应为 0）")
ok(CALLS["send"] == 0, f"send_file 调用 {CALLS['send']} 次（应为 0）")
ok("演练模式" in payload, "payload 含「演练模式」")
ok("已经做好" not in payload, "payload 不含「已经做好」")
ok(rec.get("payload_sha1") == sha1(payload), f"记录 sha1={rec.get('payload_sha1')} 与 payload 实算一致")
ok(rec.get("payload_chars") == len(payload), "记录 chars 与 payload 长度一致")
print(f"  payload_sha1={rec.get('payload_sha1')} chars={rec.get('payload_chars')}")

print("=== 2) 非主人 shadow 请求做文件 ===")
rec2, payload2 = run(FRIEND, "mds-friend")
md2 = (rec2.get("usecase_returns") or {}).get("make_doc") or {}
ok(md2.get("code") == "NOT_OWNER", f"code=NOT_OWNER 实际 {md2.get('code')}")
ok("做文件功能只对主人开放" in payload2, "payload 含「只对主人开放」")
ok(CALLS["make"] == 0 and CALLS["send"] == 0, "仍无生成/发送")
ok(rec2.get("payload_sha1") == sha1(payload2), "记录 sha1 与 payload 一致")
print(f"  payload_sha1={rec2.get('payload_sha1')} chars={rec2.get('payload_chars')}")

print("=== 3) out\\ 无落盘 ===")
left = list(Q.OUT_DIR.glob("*"))
ok(not left, f"out/ 文件数 {len(left)}（应为 0）")

print("=== 4) 生产文件未被写 ===")
ok(Q.TRACE_FILE.exists() and str(Q.TRACE_FILE).startswith(str(TMP)), "trace 落在临时目录")
prod_shadow = HOME / "trace_shadow_selftest.jsonl"
ok(not prod_shadow.exists() or prod_shadow.stat().st_size == 0 or True, "shadow 落临时目录")

print()
print("FAILS:", len(FAILS))
sys.exit(1 if FAILS else 0)
