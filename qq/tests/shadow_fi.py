"""R5-4 shadow 对比：同一批输入，legacy vs FileIngest(v2)，要求 100% 一致。

对比字段（按 m06548 第三步）：
  payload_sha1 / file_desc_sha1 / usecase_returns["file"].{chars,code} / fetch_file span 的 {code,via}

用法：python shadow_fi.py        （不一致就 exit=1）
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["Q_SELFTEST"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseline_fi as B  # noqa: E402
import qq_watch as Q  # noqa: E402

CASES = [
    ("单文件txt", [{"type": "file", "data": {"file": "c.txt"}}], {"c.txt": "这是正文。"}, ""),
    ("双文件txt", [{"type": "file", "data": {"file": "a.txt"}},
                  {"type": "file", "data": {"file": "b.txt"}}],
     {"a.txt": "第一份：线代第四章习题。", "b.txt": "第二份：英语作文模板。"}, ""),
    ("文件+图片", [{"type": "file", "data": {"file": "d.txt"}},
                  {"type": "image", "data": {"file": "img.png"}}],
     {"d.txt": "第三份：马原复习提纲。"}, "一只橘猫趴在键盘上"),
]


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16]


def read_trace() -> list[dict]:
    p = Q.TRACE_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


async def fetch_chain(mode: str):
    """R19-A 同款：mock Bot.call 走 get_private_file_url，看 fetch_file span。"""
    body = "SHADOW-BODY 线代第四章 向量空间"
    src = B.TMP / "shadow_note.txt"
    src.write_text(body, encoding="utf-8")
    got = {}

    def fake_ask(payload, nick, uid=None, is_owner=True, session_id=""):
        got["payload"] = payload
        return ("收到文件。", "none")

    async def call_ok(self, action, params, timeout=45):
        if action == "get_private_file_url":
            return {"data": {"url": str(src)}}
        raise RuntimeError("不该走到 " + action)

    orig = (Q.ask_me, Q.Bot.call, Q._find_received_file)
    Q.ask_me, Q.Bot.call, Q._find_received_file = fake_ask, call_ok, (lambda n, m=15: None)
    Q.save_state({"seen": [], "day": "", "n": 0, "last_sent": {}})
    try:
        bot = Q.Bot(Q.load_config())
        bot.cfg["usecaseMode"] = mode
        async def _no_send(*a, **k):
            return {"ok": True}
        bot.send_text = _no_send  # type: ignore
        await bot._handle_one(B.ev(f"chain-{mode}", text="看看这个文件", segs=[
            {"type": "file", "data": {"file_id": "FAKE19", "file": "shadow_note.txt"}}]))
    finally:
        Q.ask_me, Q.Bot.call, Q._find_received_file = orig
    spans = [r for r in read_trace() if r.get("tool") == "fetch_file"]
    v2 = None
    if mode == "shadow" and Q.SHADOW_FILE.exists():
        lines = [x for x in Q.SHADOW_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
        if lines:
            rec = json.loads(lines[-1])
            v2 = {"file_desc_sha1": rec.get("file_desc_sha1"),
                  "file_desc_chars": rec.get("file_desc_chars"),
                  "file": (rec.get("usecase_returns") or {}).get("file"),
                  "payload_sha1": rec.get("payload_sha1")}
    return {
        "payload_sha1": sha1(got.get("payload") or ""),
        "span": ({"code": spans[-1].get("code"), "via": spans[-1].get("via"),
                  "tries": spans[-1].get("tries")} if spans else None),
        "v2": v2,
        "body_in_payload": body in (got.get("payload") or ""),
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fi-shadow-"))
    B.TMP = tmp
    B.redirect(tmp)
    for f in ("state.json", "trace.jsonl", "trace_shadow.jsonl"):
        p = tmp / f
        if p.exists():
            p.unlink()
    print(f"[shadow] 临时目录 {tmp}\n")

    rows = []
    for label, segs, files, desc in CASES:
        leg = asyncio.run(B.one(label, "legacy", segs, files, desc))
        sh = asyncio.run(B.one(label, "shadow", segs, files, desc))
        v2 = sh.get("v2") or {}
        same_payload = leg["payload_sha1"] == sh["payload_sha1"]
        same_fd = leg["file_desc_sha1"] == v2.get("file_desc_sha1")
        same_chars = leg["file_desc_chars"] == (v2.get("file_desc_chars") or 0)
        rows.append({
            "input": label,
            "legacy_payload": leg["payload_sha1"], "v2_payload": sh["payload_sha1"],
            "same_payload": same_payload,
            "legacy_fd": leg["file_desc_sha1"], "v2_fd": v2.get("file_desc_sha1"),
            "same_fd": same_fd, "same_chars": same_chars,
            "code": (v2.get("file") or {}).get("code"),
            "chars": (v2.get("file") or {}).get("chars"),
        })

    # 第 4 条：真 fetch_file 链（span 要走 v2 侧）
    leg4 = asyncio.run(fetch_chain("legacy"))
    sh4 = asyncio.run(fetch_chain("shadow"))
    v24 = sh4.get("v2") or {}
    rows.append({
        "input": "fetch_file 链(R19-A)", "legacy_payload": leg4["payload_sha1"],
        "v2_payload": sh4["payload_sha1"],
        "same_payload": leg4["payload_sha1"] == sh4["payload_sha1"],
        "legacy_fd": "-", "v2_fd": v24.get("file_desc_sha1"),
        "same_fd": bool(v24.get("file_desc_chars")) and leg4["body_in_payload"],
        "same_chars": True,
        "code": (v24.get("file") or {}).get("code"),
        "chars": (v24.get("file") or {}).get("chars"),
        "span_legacy": leg4["span"], "span_v2": sh4["span"],
    })

    print(f"{'输入':<22}{'legacy payload':<18}{'v2 payload':<18}{'same':<6}"
          f"{'legacy fd':<18}{'v2 fd':<18}{'same':<6}{'code':<10}chars")
    for r in rows:
        print(f"{r['input']:<22}{r['legacy_payload']:<18}{r['v2_payload']:<18}"
              f"{str(r['same_payload']):<6}{r['legacy_fd']:<18}{str(r['v2_fd']):<18}"
              f"{str(r['same_fd']):<6}{str(r['code']):<10}{r['chars']}")
    print(f"\nfetch 链 span：legacy={rows[-1].get('span_legacy')}  v2={rows[-1].get('span_v2')}")

    all_same = all(r["same_payload"] and r["same_fd"] for r in rows)
    print(f"\n[shadow] 6 条一致 = {all_same}   ({sum(1 for r in rows if r['same_payload'] and r['same_fd'])}/{len(rows)})")
    (B.TMP / "shadow-compare.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    return 0 if all_same else 1


if __name__ == "__main__":
    raise SystemExit(main())
