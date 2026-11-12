# -*- coding: utf-8 -*-
"""微信全量归档：直接读本地加密库（wechatauto-replica 取密钥 + 解密），不碰界面。

用法：
    python wx_archive.py --sync        增量同步（首次会自动全量）
    python wx_archive.py --full        强制全量重扫
    python wx_archive.py --stats       看归档了多少
    python wx_archive.py --group 示例分组B   看某个群最近 N 条
    python wx_archive.py --chats       列全部会话

产物：C:\\Users\\yourname\\.dsh\\wechat-read\\archive.sqlite
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path(r"C:\Users\yourname\.dsh\wechat-read")
HOME.mkdir(parents=True, exist_ok=True)
DB_PATH = HOME / "archive.sqlite"
WORKDIR = HOME / "wadb"
LOG = HOME / "wx_archive.log"

PAGE = 200
MAX_PAGES_PER_CHAT = 400   # 防止某个巨长会话卡死（400*200 = 8 万条/会话）


def log(msg: str) -> None:
    line = time.strftime("%m-%d %H:%M:%S ") + msg
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_focus() -> dict:
    p = HOME / "focus.json"
    if not p.exists():
        return {"enabled": False, "groups": []}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8-sig"))
        return {"enabled": bool(cfg.get("enabled")),
                "groups": [str(g) for g in (cfg.get("groups") or []) if str(g).strip()]}
    except Exception:
        return {"enabled": False, "groups": []}


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def in_focus(name: str) -> bool:
    """会话名是否在重点名单里（容忍截图/OCR 截断：双向包含 + 相似度兜底）。"""
    cfg = load_focus()
    if not cfg["enabled"]:
        return True
    t = _norm(name)
    if not t:
        return False
    for g in cfg["groups"]:
        n = _norm(g)
        if not n:
            continue
        if n in t or t in n:
            return True
        import difflib
        if difflib.SequenceMatcher(None, n, t).ratio() >= 0.72:
            return True
    return False


def open_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS chats(
        username TEXT PRIMARY KEY,
        name TEXT,
        kind TEXT,
        last_ts INTEGER,
        archived_ts INTEGER,
        n_msg INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS messages(
        username TEXT,
        local_id INTEGER,
        ts INTEGER,
        sender TEXT,
        ctype TEXT,
        text TEXT,
        raw TEXT,
        PRIMARY KEY (username, local_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_username ON messages(username, ts)")
    con.execute("""CREATE TABLE IF NOT EXISTS nicknames(
        username TEXT PRIMARY KEY,
        nick TEXT)""")
    con.commit()
    return con


XML_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
SENDER_PREFIX = re.compile(r"^(wxid_[0-9A-Za-z_-]+|[0-9]+@chatroom|[A-Za-z0-9_-]+@openim):\n")


def clean_message(content: str, ctype: str):
    """拆出（发送者, 可读文本）。群消息原始内容以 wxid_xxx:\\n 开头。"""
    sender = ""
    body = content or ""
    m = SENDER_PREFIX.match(body)
    if m:
        sender = m.group(1)
        body = body[m.end():]
    if body.lstrip().startswith("<?xml") or "<appmsg" in body:
        t = XML_TITLE.search(body)
        if t and t.group(1).strip():
            body = "[%s] %s" % (ctype, t.group(1).strip())
        else:
            body = "[%s]" % ctype
    body = body.strip()
    if len(body) > 4000:
        body = body[:4000] + "…"
    return sender, body


def get_db():
    from wechatauto import WeChatDB
    WORKDIR.mkdir(parents=True, exist_ok=True)
    return WeChatDB(workdir=str(WORKDIR))


def chat_name(db, username: str, sess: dict) -> str:
    try:
        n = db.get_nickname(username)
        if n:
            return n
    except Exception:
        pass
    if username.endswith("@chatroom"):
        try:
            g = db.group_id_to_name(username)
            if g:
                return g
        except Exception:
            pass
    return username


def kind_of(username: str, name: str) -> str:
    if username.endswith("@chatroom"):
        return "group"
    if username in ("filehelper",):
        return "file"
    if username.startswith("gh_") or username.endswith(("newsapp", "brandsessionholder", "weixin")):
        return "official"
    return "person"


def sync(full: bool = False, only: str = ""):
    db = get_db()
    con = open_db()
    log("取密钥并枚举会话…")
    sessions = db.get_sessions(1000)
    log("会话数：%d" % len(sessions))

    # 顺手把「wxid → 昵称」存下来，回看时才是人名而不是 wxid
    try:
        nm = db.nickname_map()
        if nm:
            rows = [(str(k), str(v)) for k, v in nm.items() if v]
            con.executemany("INSERT INTO nicknames(username,nick) VALUES(?,?) "
                            "ON CONFLICT(username) DO UPDATE SET nick=?", 
                            [(u, n, n) for u, n in rows])
            con.commit()
            log("昵称表：%d 条" % len(rows))
    except Exception as e:
        log("昵称表跳过：%s" % e)

    total_new = 0
    skipped = 0
    for i, s in enumerate(sessions, 1):
        username = s.get("username") or ""
        if not username:
            continue
        name = chat_name(db, username, s)
        if only and only not in name and only not in username:
            continue
        # 白名单闸：开着 focus 就只同步重点会话，别的不进库
        if not load_focus().get("enabled") is False and not in_focus(name):
            skipped += 1
            continue

        row = con.execute("SELECT n_msg, last_ts FROM chats WHERE username=?", (username,)).fetchone()
        have = 0 if full else (row[0] if row else 0)
        if not full and row and row[1] and s.get("last_time") and row[1] >= int(s.get("last_time") or 0) and have:
            continue  # 这个会话没更新

        new_msgs = []
        offset = 0
        for _ in range(MAX_PAGES_PER_CHAT):
            try:
                batch = db.get_messages(username, limit=PAGE, offset=offset)
            except Exception as e:
                log("  读 %s 失败：%s" % (name, e))
                break
            if not batch:
                break
            new_msgs.extend(batch)
            if len(batch) < PAGE:
                break
            offset += PAGE
            # 增量模式：翻到已经归档过的地方就停
            if not full and have:
                ids = [m.get("local_id") for m in batch]
                if ids and min(x for x in ids if x is not None) <= 1:
                    break
                if row:
                    oldest_have = con.execute(
                        "SELECT MIN(local_id) FROM messages WHERE username=?", (username,)).fetchone()[0]
                    if oldest_have is not None and ids and min(i for i in ids if i is not None) <= oldest_have:
                        break

        if not full and have:
            # 只保留比库里最老一条更新的（去重靠主键，但少写点也行）
            pass

        rows = []
        for m in new_msgs:
            lid = m.get("local_id")
            if lid is None:
                continue
            sender, text = clean_message(m.get("content", ""), m.get("type", ""))
            rows.append((username, int(lid), int(m.get("create_time") or 0), sender,
                         m.get("type") or "", text, (m.get("content") or "")[:4000]))
        if rows:
            con.executemany("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?)", rows)
            total_new += len(rows)

        total = con.execute("SELECT COUNT(*) FROM messages WHERE username=?", (username,)).fetchone()[0]
        last_ts = con.execute("SELECT MAX(ts) FROM messages WHERE username=?", (username,)).fetchone()[0] or 0
        con.execute("INSERT INTO chats(username,name,kind,last_ts,archived_ts,n_msg) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(username) DO UPDATE SET name=?,kind=?,last_ts=?,archived_ts=?,n_msg=?",
                    (username, name, kind_of(username, name), last_ts, int(time.time()), total,
                     name, kind_of(username, name), last_ts, int(time.time()), total))
        con.commit()
        if len(rows):
            log("[%d/%d] %s：抓 %d 条，库内共 %d" % (i, len(sessions), name[:24], len(rows), total))

    log("同步结束，新增 %d 条（跳过非重点会话 %d 个）" % (total_new, skipped))
    con.close()


def prune(force: bool = False):
    """把不在重点名单里的会话和它的消息从库里删掉。"""
    if not load_focus().get("enabled"):
        print("白名单没开，先开 focus.json 再清")
        return
    con = open_db()
    rows = con.execute("SELECT username,name,n_msg FROM chats").fetchall()
    drop = [(u, n, c) for u, n, c in rows if not in_focus(n or "")]
    keep = [(u, n, c) for u, n, c in rows if in_focus(n or "")]
    print("保留 %d 个会话，删除 %d 个" % (len(keep), len(drop)))
    if not force:
        for u, n, c in drop[:15]:
            print("  会删：%-26s %6d 条" % ((n or u)[:26], c))
        print("（加 --yes 真删）")
        con.close()
        return
    before = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    for u, n, c in drop:
        con.execute("DELETE FROM messages WHERE username=?", (u,))
        con.execute("DELETE FROM chats WHERE username=?", (u,))
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con.execute("VACUUM")
    con.close()
    print("删掉消息 %d 条，剩 %d 条；库 %.1f MB" % (before - after, after, DB_PATH.stat().st_size / 1048576))


KEYWORDS = ("作业", "实验", "报告", "论文", "提交", "截止", "ddl", "DDL", "deadline", "交到",
            "前交", "考试", "测验", "报名", "签到", "打卡", "完成", "任务", "需要", "请",
            "通知", "会议", "上课", "取消", "改期", "地点", "时间", "组队", "分组", "上传",
            "评分", "平时分", "预习", "复习", "code", "homework", "assignment")


def digest(days: int = 30, per_chat: int = 60, limit_chars: int = 7000) -> str:
    """近 N 天重点群里「像任务/通知」的消息，按会话分组、时间升序。"""
    con = open_db()
    since = int(time.time()) - days * 86400
    NICK = {u: n for u, n in con.execute("SELECT username,nick FROM nicknames").fetchall() if n}
    chats = con.execute("SELECT username,name FROM chats WHERE last_ts>=? ORDER BY last_ts DESC",
                        (since,)).fetchall()
    out, total = [], 0
    for username, name in chats:
        rows = con.execute(
            "SELECT ts,sender,text FROM messages WHERE username=? AND ts>=? ORDER BY ts ASC",
            (username, since)).fetchall()
        hit = []
        for ts, sender, text in rows:
            if not text:
                continue
            low = text.lower()
            if any(k.lower() in low for k in KEYWORDS):
                hit.append((ts, NICK.get(sender or "", sender or ""), text))
        if not hit:
            continue
        hit = hit[-per_chat:]
        out.append("\n【%s】(%d 条命中)" % (name, len(hit)))
        for ts, sender, text in hit:
            t = time.strftime("%m-%d %H:%M", time.localtime(ts))
            line = "  %s %s: %s" % (t, (sender or "")[:12], text.replace("\n", " ")[:180])
            if total + len(line) > limit_chars:
                out.append("  …（后面还有，先截到这里）")
                con.close()
                return "\n".join(out)
            out.append(line)
            total += len(line)
    con.close()
    return "\n".join(out) if out else "近 %d 天重点群里没找到像任务/通知的消息。" % days


def stats():
    if not DB_PATH.exists():
        print("还没有归档")
        return
    con = open_db()
    n_chat = con.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    n_msg = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print("会话 %d 个，消息 %d 条，库 %.1f MB" % (n_chat, n_msg, DB_PATH.stat().st_size / 1048576))
    print("\n消息最多的 15 个会话：")
    for name, kind, n in con.execute(
            "SELECT name, kind, n_msg FROM chats ORDER BY n_msg DESC LIMIT 15"):
        print("  %-28s %-6s %6d" % ((name or "")[:28], kind, n))
    con.close()


def show(only: str, limit: int = 40, force: bool = False):
    con = open_db()
    rows = con.execute("SELECT username,name FROM chats WHERE name LIKE ? OR username LIKE ? "
                       "ORDER BY n_msg DESC LIMIT 20",
                       ("%" + only + "%", "%" + only + "%")).fetchall()
    if not rows:
        print("库里没这个会话：", only)
        return
    # 白名单闸：候选里挑第一个在重点名单内的
    pick = None
    for username, name in rows:
        if force or in_focus(name):
            pick = (username, name)
            break
    if not pick:
        print("不在重点名单里（当前只读那几组）：%s" % ", ".join(n for _, n in rows[:3]))
        return
    username, name = pick
    print("== %s ==" % name)
    for ts, sender, nick, ctype, text in con.execute(
            "SELECT m.ts, m.sender, COALESCE(n.nick, m.sender), m.ctype, m.text "
            "FROM messages m LEFT JOIN nicknames n ON n.username = m.sender "
            "WHERE m.username=? ORDER BY m.ts DESC LIMIT ?",
            (username, limit)):
        t = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        who = (nick or "")[:12]
        print("%s %-12s %s" % (t, who, (text or "")[:120]))
    con.close()


def list_chats():
    con = open_db()
    for name, kind, n in con.execute("SELECT name, kind, n_msg FROM chats ORDER BY last_ts DESC"):
        print("%-8s %-30s %5d" % (kind, (name or "")[:30], n))
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--chats", action="store_true")
    ap.add_argument("--group", default="", help="看某个会话最近 N 条")
    ap.add_argument("--only", default="", help="同步时只处理名字含这个词的会话")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--loop", action="store_true", help="常驻增量同步")
    ap.add_argument("--interval", type=float, default=600.0)
    ap.add_argument("--prune", action="store_true", help="删掉不在白名单里的会话")
    ap.add_argument("--yes", action="store_true", help="配合 --prune 真删")
    ap.add_argument("--digest", type=int, default=0, help="近 N 天重点群任务/通知，按群分组输出")
    args = ap.parse_args()

    if args.digest:
        print(digest(args.digest))
    elif args.prune:
        prune(args.yes)
    elif args.stats:
        stats()
    elif args.chats:
        list_chats()
    elif args.group and not args.sync and not args.full:
        show(args.group, args.limit)
    elif args.loop:
        while True:
            t0 = time.time()
            try:
                sync(full=False)
            except Exception as e:
                log("同步出错：%s: %s" % (type(e).__name__, e))
            spent = time.time() - t0
            time.sleep(max(30.0, args.interval - spent))
    else:
        sync(full=args.full or False, only=args.only)


if __name__ == "__main__":
    main()
