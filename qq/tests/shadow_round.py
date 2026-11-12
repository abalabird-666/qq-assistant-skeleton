# shadow_round.py -- R5-3 shadow 一轮演练（自动回切方案：主人真回复 / 非主人静默）
#
# 为什么是进程内演练：3001（机器人号 NapCat）当前停在扫码界面，真链路收不到消息，
# 所以用一个内存里的 Bot 走**同一套 _handle_one → _handle_one_v2** 代码，
# 只把「发出去」换成记录。等 3001 上线后再用真链路复跑（脚本见文件尾注释）。
import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ["Q_SELFTEST"] = "1"          # 日志/trace 落 selftest 文件，不写生产 trace.jsonl
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import qq_watch as Q  # noqa: E402

HOME = Path(r"C:\Users\yourname\.dsh\qq-duty")
TMP = HOME / "_shadow_round_tmp"
PROD_TRACE = HOME / "trace.jsonl"


def ev(uid, text, mid, nick=""):
    return {
        "post_type": "message", "message_type": "private", "user_id": uid,
        "message_id": mid, "sender": {"nickname": nick or str(uid)},
        "message": [{"type": "text", "data": {"text": text}}],
    }


async def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    for f in TMP.iterdir():
        if f.is_file():
            f.unlink()

    # 只把「会被写」的状态挪走；trace 由 Q_SELFTEST 自动落到 trace_selftest.jsonl
    Q.STATE = TMP / "state.json"
    Q.DOCS_DB = TMP / "docs.json"
    Q.LAST_DOC = TMP / "last_doc.json"
    Q.FILES_DIR = TMP / "files"
    Q.OUT_DIR = TMP / "out"
    Q.SESSION_FILE = TMP / "qq-session.json"
    Q.WX_FOCUS = TMP / "focus.json"
    Q.session_file = lambda uid: TMP / f"qq-session-{int(uid)}.json"
    Q.FILES_DIR.mkdir(exist_ok=True)
    Q.OUT_DIR.mkdir(exist_ok=True)
    Q.WX_FOCUS.write_text(json.dumps({"groups": ["旧群A"], "enabled": True}, ensure_ascii=False),
                          encoding="utf-8")

    sent, asked = [], []

    async def fake_send_text(self, user_id, text, dedup=True):
        sent.append((int(user_id), text))
        return Q.SendResult(ok=True, skipped=False, verifying=False)

    def fake_ask(payload, nick, uid=None, is_owner=True, session_id=""):
        asked.append((int(uid or 0), is_owner, len(payload)))
        return ("我这边挺好的，你呢？", "none")

    orig_send, orig_ask = Q.Bot.send_text, Q.ask_me
    Q.Bot.send_text = fake_send_text
    Q.ask_me = fake_ask

    prod_before = PROD_TRACE.stat().st_size if PROD_TRACE.exists() else 0
    shadow_file = Q.SHADOW_FILE
    shadow_before = shadow_file.stat().st_size if shadow_file.exists() else 0
    t0 = time.time()
    try:
        bot = Q.Bot(Q.load_config())
        bot.state = {"seen": [], "day": "", "n": 0, "last_sent": {}}
        bot.cfg["usecaseMode"] = "shadow"

        # A 主人闲聊 → 必须真回复
        await bot._handle_one(ev(100000001, "今天有点累，随便聊两句", "shadowA"))
        asked_a, sent_a = len(asked), len(sent)

        # B 主人指令（白名单）→ shadow 只判不做，副作用仍由 legacy 执行一次
        await bot._handle_one(ev(100000001, "白名单改成 示例分组B", "shadowB"))
        focus_after = json.loads(Q.WX_FOCUS.read_text(encoding="utf-8"))["groups"]
        asked_b, sent_b = len(asked), len(sent)   # 指令分支自己会回一条，基线要从这里取

        # C 非主人闲聊 → 静默
        await bot._handle_one(ev(100000002, "在吗", "shadowC", nick="CR"))
        asked_c, sent_c = len(asked), len(sent)
    finally:
        Q.Bot.send_text, Q.ask_me = orig_send, orig_ask

    prod_after = PROD_TRACE.stat().st_size if PROD_TRACE.exists() else 0
    recs = []
    if shadow_file.exists():
        with shadow_file.open("rb") as f:
            f.seek(shadow_before)
            for line in f.read().decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass

    print("== shadow 一轮（进程内，同一套 _handle_one 代码）==")
    print(f"  耗时 {time.time() - t0:.1f}s   shadow 文件 {shadow_file.name}")
    for r in recs:
        print(f"  - mid={r.get('mid')} mode={r.get('mode')} command_kind={r.get('command_kind')!r} "
              f"payload_sha1={r.get('payload_sha1')!r} chars={r.get('payload_chars')} "
              f"reply_skipped={r.get('reply_skipped')} usecase_returns={r.get('usecase_returns')}")
    print(f"  生产 trace.jsonl: {prod_before} B -> {prod_after} B (差 {prod_after - prod_before} B)")
    print(f"  ask_me 调用: A 之后 {asked_a} / B 之后 {asked_b} / C 之后 {asked_c}")
    print(f"  发出去: A 之后 {sent_a} / B 之后 {sent_b} / C 之后 {sent_c}（C 段应等于 B 段）")
    print(f"  focus.json: {focus_after}（shadow 下由 legacy 执行一次）")

    by_mid = {r.get("mid"): r for r in recs}
    a, b, c = by_mid.get("shadowA", {}), by_mid.get("shadowB", {}), by_mid.get("shadowC", {})
    checks = [
        ("A 主人有真回复", asked_a == 1 and sent_a == 1),
        ("B 指令被识别", b.get("command_kind") == "whitelist"),
        ("B 无 payload（短路语义）", b.get("payload_chars") == 0),
        ("B 副作用只发生一次", focus_after == ["示例分组B"]),
        ("C 非主人静默（模型/发送都不增）", asked_c == asked_b and sent_c == sent_b),
        ("C shadow 记录 reply_skipped", bool(c.get("reply_skipped"))),
        ("生产 trace 未被写入", prod_after == prod_before),
    ]
    print("== 断言 ==")
    bad = 0
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

# 3001 上线后可用真链路复跑：
#   python dev_send.py text "今天有点累"        # 以主人号(3002) 发给机器人号(3001)
#   python dev_send.py text "白名单改成 示例分组B"
# 然后看 qq-duty/trace_shadow.jsonl 与 qq-duty/trace.jsonl 的增量。
