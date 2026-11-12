#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 聊天记录归档 + 摘要（走 NapCat 的 OneBot WS，不用截图、不用 OCR）。

用法：
    python qq_archive.py --groups            # 看机器人加入的群/好友
    python qq_archive.py --sync              # 增量归档（默认）
    python qq_archive.py --full              # 全量重灌
    python qq_archive.py --stats             # 库概况
    python qq_archive.py --chats             # 会话列表
    python qq_archive.py --group 示例分组B --limit 30
    python qq_archive.py --digest 30         # 近 30 天任务/通知原文（按会话分组）
    python qq_archive.py --loop --interval 600
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

try:
    import aiohttp
except Exception:  # pragma: no cover
    print("缺 aiohttp")
    raise

HOME = Path(r"C:\Users\yourname\.dsh\qq-duty")
CFG = HOME / "config.json"
DB_PATH = HOME / "qq_archive.sqlite"
FOCUS = HOME / "qq_focus.json"
LOG = HOME / "qq_archive.log"

PAGE = 200            # 单次拉多少条
MAX_PAGES = 60        # 单个会话最多翻多少页（1.2 万条）
SELF = 0

CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


_WS_OVERRIDE = None


def ws_url() -> str:
    if _WS_OVERRIDE:
        return _WS_OVERRIDE
    c = cfg()
    return c.get("wsUrl") or "ws://127.0.0.1:3001"


def ws_me() -> str:
    """「我自己的号」那份 NapCat（3002）。"""
    return "ws://127.0.0.1:3002"


def token() -> str:
    return cfg().get("token") or ""


# ---------------- 消息文本 ----------------
def text_of(message) -> str:
    """OneBot 消息段/字符串 → 可读文本。"""
    if message is None:
        return ""
    if isinstance(message, str):
        return message.strip()
    parts = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type") or ""
        d = seg.get("data") or {}
        if t == "text":
            parts.append(str(d.get("text") or ""))
        elif t == "image":
            parts.append("[图片]")
        elif t == "face":
            parts.append("[表情]")
        elif t == "record":
            parts.append("[语音]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "file":
            parts.append("[文件]")
        elif t == "at":
            parts.append("@" + str(d.get("qq") or ""))
        elif t == "reply":
            continue
        elif t == "forward":
            parts.append("[合并转发]")
        else:
            parts.append("[%s]" % t)
    return "".join(parts).strip()


# ---------------- OneBot WS ----------------
class Api:
    def __init__(self, url: str, tok: str):
        self.url = url
        self.tok = tok
        self.session = None
        self.ws = None
        self.seq = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.reader = None

    async def __aenter__(self):
        headers = {"Authorization": "Bearer %s" % self.tok} if self.tok else {}
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url, headers=headers, heartbeat=30)
        self.reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *a):
        try:
            if self.reader:
                self.reader.cancel()
            if self.ws:
                await self.ws.close()
            if self.session:
                await self.session.close()
        except Exception:
            pass

    async def _read_loop(self):
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                echo = data.get("echo")
                if echo and echo in self.pending and not self.pending[echo].done():
                    self.pending[echo].set_result(data)
        except asyncio.CancelledError:
            pass

    async def call(self, action: str, params: dict, timeout: float = 60.0) -> dict:
        self.seq += 1
        echo = "q%d" % self.seq
        fut = asyncio.get_event_loop().create_future()
        self.pending[echo] = fut
        try:
            await self.ws.send_json({"action": action, "params": params, "echo": echo})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log("调用 %s 超时" % action)
            return {}
        except Exception as e:
            log("调用 %s 失败：%s: %s" % (action, type(e).__name__, e))
            return {}
        finally:
            self.pending.pop(echo, None)


# ---------------- 白名单 ----------------
def load_focus() -> dict:
    if not FOCUS.exists():
        return {"enabled": False, "groups": []}
    try:
        c = json.loads(FOCUS.read_text(encoding="utf-8"))
        return {"enabled": bool(c.get("enabled")),
                "groups": [str(x) for x in (c.get("groups") or []) if str(x).strip()]}
    except Exception:
        return {"enabled": False, "groups": []}


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def in_focus(name: str) -> bool:
    f = load_focus()
    if not f["enabled"]:
        return True
    t = _norm(name)
    if not t:
        return False
    for g in f["groups"]:
        n = _norm(g)
        if not n:
            continue
        if n in t or t in n:
            return True
        import difflib
        if difflib.SequenceMatcher(None, n, t).ratio() >= 0.72:
            return True
    return False


# ---------------- 库 ----------------
def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS chats(
        chat_id TEXT PRIMARY KEY, kind TEXT, name TEXT, last_ts INTEGER, n_msg INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS messages(
        chat_id TEXT, msg_id TEXT, ts INTEGER, sender_id TEXT, sender_nick TEXT,
        text TEXT, PRIMARY KEY(chat_id, msg_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_m_ts ON messages(chat_id, ts)")
    return con


async def list_targets(api: Api) -> list[dict]:
    """返回 [{chat_id, kind, name}]：机器人加入的群 + 私聊过的好友。"""
    out = []
    r = await api.call("get_group_list", {})
    for g in (r.get("data") or []):
        gid = str(g.get("group_id") or "")
        if gid:
            out.append({"chat_id": gid, "kind": "group",
                        "name": str(g.get("group_name") or gid)})
    r = await api.call("get_friend_list", {})
    for f in (r.get("data") or []):
        uid = str(f.get("user_id") or "")
        if uid:
            out.append({"chat_id": uid, "kind": "person",
                        "name": str(f.get("remark") or f.get("nickname") or uid)})
    return out


async def fetch_history(api: Api, chat_id: str, kind: str,
                        oldest_ts: int = 0, max_pages: int = MAX_PAGES,
                        days: int = 0) -> list[dict]:
    """往前翻历史，直到翻完 / 到页数上限 / 已经比库里还老 / 早于 N 天前。"""
    rows: list[dict] = []
    cutoff = int(time.time()) - days * 86400 if days else 0
    seq = 0
    for _ in range(max_pages):
        params = {"count": PAGE, "message_seq": seq}
        if kind == "group":
            params["group_id"] = chat_id
            r = await api.call("get_group_msg_history", params)
        else:
            params["user_id"] = chat_id
            r = await api.call("get_friend_msg_history", params)
        msgs = (r.get("data") or {}).get("messages") or []
        if not msgs:
            break
        batch = []
        for m in msgs:
            ts = int(m.get("time") or 0)
            mid = str(m.get("message_id") or m.get("msg_id") or "")
            if not mid:
                continue
            sender = (m.get("sender") or {})
            batch.append({
                "msg_id": mid, "ts": ts,
                "sender_id": str(sender.get("user_id") or ""),
                "sender_nick": str(sender.get("card") or sender.get("nickname") or ""),
                "text": text_of(m.get("message")),
                "raw": json.dumps(m.get("message"), ensure_ascii=False)[:4000],
            })
        if not batch:
            break
        rows.extend(batch)
        ids = [int(m["msg_id"]) for m in batch if str(m["msg_id"]).isdigit()]
        oldest = min(ids) if ids else 0
        oldest_in_batch = min(m["ts"] for m in batch) if batch else 0
        if oldest_ts and oldest_in_batch and oldest_in_batch <= oldest_ts:
            break
        if cutoff and oldest_in_batch and oldest_in_batch < cutoff:
            break
        if not oldest or oldest <= 1:
            break
        seq = oldest
        if len(msgs) < PAGE:
            break
    return rows


async def sync(full: bool = False, days: int = 0, max_pages: int = MAX_PAGES) -> None:
    con = open_db()
    async with Api(ws_url(), token()) as api:
        targets = await list_targets(api)
        log("会话数：%d（群 %d）" % (len(targets), sum(1 for t in targets if t["kind"] == "group")))
        skipped = 0
        total_new = 0
        for t in targets:
            if not in_focus(t["name"]):
                skipped += 1
                continue
            row = con.execute("SELECT last_ts, n_msg FROM chats WHERE chat_id=?",
                              (t["chat_id"],)).fetchone()
            oldest = 0
            if not full and row and row[1]:
                oldest = con.execute("SELECT MAX(ts) FROM messages WHERE chat_id=?",
                                     (t["chat_id"],)).fetchone()[0] or 0
            rows = await fetch_history(api, t["chat_id"], t["kind"], oldest_ts=oldest,
                                       max_pages=max_pages, days=days)
            ins = [(t["chat_id"], r["msg_id"], r["ts"], r["sender_id"],
                    r["sender_nick"], r["text"]) for r in rows if r["ts"]]
            if ins:
                con.executemany("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)", ins)
                total_new += len(ins)
            n = con.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?",
                            (t["chat_id"],)).fetchone()[0]
            last = con.execute("SELECT MAX(ts) FROM messages WHERE chat_id=?",
                               (t["chat_id"],)).fetchone()[0] or 0
            con.execute("INSERT INTO chats(chat_id,kind,name,last_ts,n_msg) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(chat_id) DO UPDATE SET kind=?,name=?,last_ts=?,n_msg=?",
                        (t["chat_id"], t["kind"], t["name"], last, n, t["kind"], t["name"], last, n))
            con.commit()
            if ins:
                log("  %s：抓 %d 条，库内 %d" % (t["name"][:24], len(ins), n))
        log("同步结束，新增 %d 条（跳过非重点 %d 个）" % (total_new, skipped))
    con.close()


# ---------------- 查询/摘要 ----------------
KEYWORDS = ("作业", "实验", "报告", "论文", "提交", "截止", "ddl", "DDL", "deadline", "交到",
            "前交", "考试", "测验", "报名", "签到", "打卡", "完成", "任务", "需要", "请",
            "通知", "会议", "上课", "取消", "改期", "地点", "时间", "组队", "分组", "上传",
            "评分", "平时分", "预习", "复习", "code", "homework", "assignment")


def show(only: str, limit: int = 40) -> None:
    con = open_db()
    rows = con.execute("SELECT chat_id,name,kind FROM chats WHERE name LIKE ? ORDER BY last_ts DESC",
                       ("%" + only + "%",)).fetchall()
    hit = [r for r in rows if in_focus(r[1] or "")]
    if not hit:
        print("库里没这个会话（或不在重点名单里）：", only)
        con.close()
        return
    chat_id, name, kind = hit[0]
    print("== %s (%s) ==" % (name, kind))
    for ts, nick, text in con.execute(
            "SELECT ts,sender_nick,text FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
            (chat_id, limit)).fetchall()[::-1]:
        print("  %s %-10s %s" % (time.strftime("%m-%d %H:%M", time.localtime(ts)),
                                 (nick or "")[:10], (text or "")[:160]))
    con.close()


def digest(days: int = 30, per_chat: int = 60, limit_chars: int = 7000) -> str:
    con = open_db()
    since = int(time.time()) - days * 86400
    out, total = [], 0
    for chat_id, name in con.execute("SELECT chat_id,name FROM chats ORDER BY last_ts DESC").fetchall():
        if not in_focus(name or ""):
            continue
        rows = con.execute("SELECT ts,sender_nick,text FROM messages WHERE chat_id=? AND ts>=? "
                           "ORDER BY ts ASC", (chat_id, since)).fetchall()
        hit = []
        for ts, nick, text in rows:
            if not text:
                continue
            low = text.lower()
            if any(k.lower() in low for k in KEYWORDS):
                hit.append((ts, nick or "", text))
        if not hit:
            continue
        hit = hit[-per_chat:]
        out.append("\n【%s】(%d 条命中)" % (name, len(hit)))
        for ts, nick, text in hit:
            line = "  %s %s: %s" % (time.strftime("%m-%d %H:%M", time.localtime(ts)),
                                    nick[:12], text.replace("\n", " ")[:180])
            if total + len(line) > limit_chars:
                out.append("  …（后面还有，先截到这里）")
                con.close()
                return "\n".join(out)
            out.append(line)
            total += len(line)
    con.close()
    return "\n".join(out) if out else "近 %d 天没找到像任务/通知的消息。" % days


def stats() -> None:
    if not DB_PATH.exists():
        print("还没有归档（先跑 --sync，需要 QQ 在线）")
        return
    con = open_db()
    n_chat = con.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    n_msg = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print("会话 %d 个，消息 %d 条，库 %.1f MB" % (n_chat, n_msg, DB_PATH.stat().st_size / 1048576))
    for name, kind, n in con.execute("SELECT name,kind,n_msg FROM chats ORDER BY n_msg DESC LIMIT 15"):
        print("  %-8s %-26s %6d" % (kind, (name or "")[:26], n))
    con.close()


def list_chats() -> None:
    con = open_db()
    for name, kind, n, last in con.execute(
            "SELECT name,kind,n_msg,last_ts FROM chats ORDER BY last_ts DESC"):
        print("%-8s %-30s %6d  %s" % (kind, (name or "")[:30], n,
                                      time.strftime("%m-%d %H:%M", time.localtime(last or 0))))
    con.close()


async def main_async(args) -> None:
    if args.groups:
        async with Api(ws_url(), token()) as api:
            for t in await list_targets(api):
                mark = "重点" if in_focus(t["name"]) else "  —"
                print("%-8s %-30s %s" % (t["kind"], t["name"][:30], mark))
        return
    if args.sync or args.full:
        await sync(full=args.full, days=args.days, max_pages=args.max_pages)
        return
    while True:
        t0 = time.time()
        try:
            await sync(full=False, days=args.days, max_pages=args.max_pages)
        except Exception as e:
            log("同步出错：%s: %s" % (type(e).__name__, e))
        spent = time.time() - t0
        await asyncio.sleep(max(30.0, args.interval - spent))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", action="store_true", help="列出机器人加入的群/好友")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--chats", action="store_true")
    ap.add_argument("--group", default="")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--digest", type=int, default=0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=600.0)
    ap.add_argument("--ws", default="", help="端口，如 3002（我自己的号那份 NapCat）")
    ap.add_argument("--days", type=int, default=0, help="只翻最近 N 天（0=不限）")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES, help="单会话最多翻几页")
    args = ap.parse_args()

    global _WS_OVERRIDE
    if args.ws:
        port = str(args.ws).replace("ws://127.0.0.1:", "").replace(":", "")
        _WS_OVERRIDE = "ws://127.0.0.1:%s" % port

    if args.stats:
        stats()
    elif args.chats:
        list_chats()
    elif args.digest:
        print(digest(args.digest))
    elif args.group:
        show(args.group, args.limit)
    else:
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
