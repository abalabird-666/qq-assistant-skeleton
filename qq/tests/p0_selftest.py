"""P0/P0.5 自测：合批、串行、幂等、假失败回读（同步/异步）、trace merged_mids、防双 flush。

不连 NapCat、不调模型；全部用假的 call/ask_me。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["Q_SELFTEST"] = "1"          # R9.5-3：日志/trace 不许写进生产文件
sys.path.insert(0, r"C:\Users\yourname\.dsh\qq-duty")
import qq_watch as Q  # noqa: E402

UID = 100000001
tmp = Path(tempfile.mkdtemp(prefix="p05test_"))
Q.STATE = tmp / "state.json"
Q.TRACE_FILE = tmp / "trace.jsonl"
Q.MERGE_WINDOW_SEC = 0.4          # 测速：缩短合批窗口
Q.VERIFY_BACKOFF_SEC = 0.2        # 测速：缩短回读退避

sends = []
hist = []
fail_next = {"on": False}


async def fake_call(self, action, params, timeout=45):
    if action == "send_private_msg":
        text = params["message"][0]["data"].get("text") or "<non-text>"
        sends.append(text)
        if fail_next["on"]:
            # 假失败：报 1200，但消息其实已经发出去了
            fail_next["on"] = False
            hist.append([{"type": "text", "data": {"text": text}}])
            return {"status": "failed", "retcode": 1200, "message": "Ban state forbit write req"}
        return {"status": "ok", "retcode": 0, "data": {"message_id": str(len(sends))}}
    if action == "get_friend_msg_history":
        return {"status": "ok", "retcode": 0,
                "data": {"messages": [{"message": m} for m in hist]}}
    return {"status": "ok", "retcode": 0, "data": {}}


async def fake_send_image(self, uid, path):
    sends.append(f"<image {Path(path).name}>")
    return True


async def fake_send_file(self, uid, path):
    sends.append(f"<file {Path(path).name}>")
    return True


def echo_ask(user_text, nick, session_id, is_owner=True):
    return (f"收到：{user_text[:20]}", "safe")


def ev(mid, segs):
    return {"post_type": "message", "message_type": "private", "user_id": UID,
            "message_id": mid, "sender": {"nickname": "测试"}, "message": segs}


def traces():
    out = []
    if Q.TRACE_FILE.exists():
        for x in Q.TRACE_FILE.read_text(encoding="utf-8").splitlines():
            if x.strip():
                out.append(json.loads(x))
    return out


async def main():
    Q.Bot.call = fake_call
    Q.Bot.send_image = fake_send_image
    Q.Bot.send_file = fake_send_file
    Q.ask_me = echo_ask
    Q._call_llm = lambda p, timeout=180: "测试印象" * 50
    Q.summarize_doc = lambda chunk_chars=0: "测试印象：这是一份测试文档的骨架。"

    bot = Q.Bot(Q.load_config())
    res = {}

    print("=" * 64)
    print("T1 合批：文件 + 文本（间隔 0.2s）→ 期望只回 1 条")
    print("=" * 64)
    sends.clear()
    await bot.handle_event(ev("1001", [{"type": "file", "data": {"file": "test.pdf"}}]))
    await asyncio.sleep(0.2)
    await bot.handle_event(ev("1002", [{"type": "text", "data": {"text": "看完这份文件"}}]))
    await asyncio.sleep(Q.MERGE_WINDOW_SEC + 3)
    print(f"  实际发送条数 = {len(sends)}  →  {sends}")
    res["T1"] = len(sends) == 1

    print()
    print("=" * 64)
    print("T2 串行：两批独立事件（间隔 2s）→ 不交叠")
    print("=" * 64)
    sends.clear()
    order = []
    orig_one = Q.Bot._handle_one

    async def spy(self, event):
        order.append("start")
        await asyncio.sleep(0.6)
        await orig_one(self, event)
        order.append("end")

    Q.Bot._handle_one = spy
    await bot.handle_event(ev("2001", [{"type": "text", "data": {"text": "你好"}}]))
    await asyncio.sleep(2)
    await bot.handle_event(ev("2002", [{"type": "text", "data": {"text": "在吗"}}]))
    await asyncio.sleep(Q.MERGE_WINDOW_SEC + 4)
    Q.Bot._handle_one = orig_one
    print(f"  调用顺序 = {order}")
    res["T2"] = order == ["start", "end", "start", "end"] and len(sends) == 2

    print()
    print("=" * 64)
    print("T3 幂等：10 秒内重复同文 → 只真发 1 次")
    print("=" * 64)
    sends.clear()
    a = await bot.send_text(UID, "这条只该出现一次")
    b = await bot.send_text(UID, "这条只该出现一次")
    print(f"  两次返回 = {dict(a)}, {dict(b)}；实际发送 = {len(sends)} 条")
    res["T3"] = len(sends) == 1 and bool(a) and bool(b)

    print()
    print("=" * 64)
    print("T4 回滚路径：verifyAsync=False 时假失败同步回读，只发 1 次")
    print("=" * 64)
    sends.clear()
    hist.clear()
    bot.state["last_sent"] = {}
    old_async = Q.VERIFY_ASYNC
    Q.VERIFY_ASYNC = False
    fail_next["on"] = True
    t0 = time.time()
    ok = await bot.send_text(UID, "同步回读测试")
    print(f"  返回 = {dict(ok)}；耗时 {time.time() - t0:.2f}s；实际发送 {len(sends)} 条")
    res["T4"] = bool(ok) and len(sends) == 1
    Q.VERIFY_ASYNC = old_async

    print()
    print("=" * 64)
    print("T5 异步回读：假失败时 send_text 立刻返回（<500ms），后台补写 state/trace")
    print("=" * 64)
    sends.clear()
    hist.clear()
    bot.state["last_sent"] = {}
    fail_next["on"] = True
    t0 = time.time()
    r = await bot.send_text(UID, "异步回读测试消息")
    cost_ms = (time.time() - t0) * 1000
    rec_now = bot.state["last_sent"][str(UID)]
    print(f"  返回 = {dict(r)}；send_text 耗时 {cost_ms:.0f}ms")
    print(f"  立即查 state：ok={rec_now.get('ok')} verifying={rec_now.get('verifying')}")
    t5_fast = cost_ms < 500 and r.get("verifying") is True and rec_now.get("verifying") is True
    await asyncio.sleep(Q.VERIFY_BACKOFF_SEC * 2 + 1.2)
    rec_after = bot.state["last_sent"][str(UID)]
    spans = [x for x in traces() if x.get("tool") == "verify_sent"]
    codes = [x.get("code") for x in spans]
    print(f"  后台完成后 state：ok={rec_after.get('ok')} verifying={rec_after.get('verifying')}")
    print(f"  trace 里的 verify_sent span：{codes}")
    res["T5"] = t5_fast and rec_after.get("ok") is True and "E_UNVERIFIED_RECOVERED" in codes

    print()
    print("=" * 64)
    print("T6 防双 flush：窗口内连注 3 条 → 同一 uid 的 flush 不交叠")
    print("=" * 64)
    sends.clear()
    order2 = []

    async def spy2(self, uid):
        order2.append("start")
        try:
            await Q.Bot._flush_once(self, uid)
        finally:
            order2.append("end")

    orig_flush = Q.Bot._flush
    Q.Bot._flush = spy2
    await bot.handle_event(ev("3001", [{"type": "text", "data": {"text": "第一条"}}]))
    await asyncio.sleep(0.05)
    await bot.handle_event(ev("3002", [{"type": "text", "data": {"text": "第二条"}}]))
    await asyncio.sleep(0.05)
    await bot.handle_event(ev("3003", [{"type": "text", "data": {"text": "第三条"}}]))
    await asyncio.sleep(Q.MERGE_WINDOW_SEC + 3)
    Q.Bot._flush = orig_flush
    print(f"  flush 调用序列 = {order2}")
    print(f"  实际发送条数 = {len(sends)}  →  {sends}")
    overlapped = any(order2[i] == "start" and order2[i + 1] == "start"
                     for i in range(len(order2) - 1))
    res["T6"] = (not overlapped) and len(sends) == 1

    print()
    print("=" * 64)
    print("T7 trace 里能 grep 到最后一条 mid，且该行含全部 mid")
    print("=" * 64)
    t1_rows = [x for x in traces() if x.get("trace") == "1002" and x.get("merged_mids")]
    print(f"  含 merged_mids=1001,1002 的行数 = {len(t1_rows)}")
    if t1_rows:
        print(f"  样例：{json.dumps(t1_rows[0], ensure_ascii=False)[:200]}")
    res["T7"] = len(t1_rows) >= 1 and all("1001" in (x.get("merged_mids") or "") and "1002" in (x.get("merged_mids") or "") for x in t1_rows)

    print()
    print("=" * 64)
    print("结果汇总")
    print("=" * 64)
    for k in ["T1", "T2", "T3", "T4", "T5", "T6", "T7"]:
        print(f"  {k}: {'PASS' if res[k] else 'FAIL'}")
    return 0 if all(res.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
