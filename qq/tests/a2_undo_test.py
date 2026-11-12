"""A2：undo 真实回滚路径取证。

做法：
  1. 备份真实 focus.json → 改前快照
  2. 造一次真实失败：白名单口令改文件成功后，把 bot.send_text 打成抛异常
  3. 断言 focus.json 回到「改前原文」（不是改动后的版本）
  4. 还原 focus.json

只劫持发送方法，不改任何业务代码。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import time

os.environ["Q_SELFTEST"] = "1"
import qq_watch as Q  # noqa: E402

FOCUS = Q.WX_FOCUS
ORIG = FOCUS.read_text(encoding="utf-8-sig") if FOCUS.exists() else None


def snap(label):
    txt = FOCUS.read_text(encoding="utf-8-sig") if FOCUS.exists() else "(不存在)"
    print(f"{label}:\n    {txt.strip()}")
    return txt


def ev(uid, text, nick="示例主人", mid=None):
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": uid,
        "message_id": mid or str(int(time.time() * 1000)),
        "sender": {"nickname": nick},
        "message": [{"type": "text", "data": {"text": text}}],
    }


async def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="a2_"))
    Q.STATE = tmp / "state.json"
    Q.TRACE_FILE = tmp / "trace.jsonl"
    Q.SESSION_FILE = tmp / "qq-session.json"
    Q.session_file = lambda uid: tmp / f"qq-session-{int(uid)}.json"

    # ① 改前
    FOCUS.write_text(json.dumps({"groups": ["旧群A"], "enabled": True}, ensure_ascii=False),
                     encoding="utf-8")
    before = snap("① 改前（focus.json 原文）")
    print("    改前 sha1:", __import__("hashlib").sha1(before.encode()).hexdigest()[:12])

    bot = Q.Bot(Q.load_config())
    bot.cfg["usecaseMode"] = "new"
    bot.cfg["qqArchiveAsCommand"] = True

    captured = {"mid": None}

    async def boom_send(user_id, text, dedup=True):
        # 发送前一刻抓一次盘上内容 —— 这就是「改后」证据
        captured["mid"] = FOCUS.read_text(encoding="utf-8-sig")
        captured["reply"] = text
        raise RuntimeError("A2 模拟发送失败")

    bot.send_text = boom_send

    raised = None
    try:
        # 直接调 _handle_one（合批/串行层之上），保证同步拿到异常
        await bot._handle_one(ev(100000001, "白名单改成 示例分组B、示例分组C", mid="a2-undo"))
    except Exception as e:  # noqa: BLE001
        raised = f"{type(e).__name__}: {e}"
    print("    _handle_one 抛出:", raised)

    print("② 改后（发送失败前一刻磁盘内容）:")
    print("    " + (captured["mid"] or "(没抓到)").strip())
    print("    要回复的那句:", (captured.get("reply") or "")[:60])

    after = snap("③ 回滚后（focus.json 原文）")
    print() 
    print("=== 断言 ===")
    print("  [%s] 改后确实变了（%s → %s）" % (
        "PASS" if captured["mid"] and captured["mid"] != before else "FAIL",
        json.loads(before), json.loads(captured["mid"] or "[]")))
    print("  [%s] 回滚后 == 改前原文" % ("PASS" if after == before else "FAIL"))
    print("  [%s] 回滚后 != 改后版本" % ("PASS" if after != captured["mid"] else "FAIL"))
    print("  [%s] 异常确实抛出去了" % ("PASS" if raised else "FAIL"))

    # 还原
    if ORIG is not None:
        FOCUS.write_text(ORIG, encoding="utf-8-sig")
        print("\n已还原 focus.json →", json.loads(ORIG))


asyncio.run(main())
