#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dm_watch.py —— 私信快线（8 秒一跳）

为什么另起一个进程：插件自带的私信监听的轮询粒度是「分钟」（dmWatchMinutes 最小 1，
代码里 setInterval(mins*60000)），主人反馈「回得偏慢」—— 60 秒才醒一次，加上起会话
+ 生成 7~8 秒，实际体感就是一分多钟才回话。

这里把「查私信」这半截单独拎出来，纯代码、不花 token：
  · 每 8 秒查一次会话列表 + 自己的会话消息
  · 有新消息才唤醒 dsh 生成一句（续用 dm-session.json 里那个会话，上下文不丢）
  · 用 bilibili_api.session.send_msg 发出去
  · 同时把 plugin 的 dm-seen.json 一起写掉，防止以后插件重新打开监听时重复回

⚠️ 启用本进程时要把插件的 dmWatch 关掉（bili_config dmWatch=false），否则两边都回。
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

import aiohttp
from bilibili_api import Credential
from bilibili_api import session as bsession

sys.path.insert(0, str(Path(__file__).resolve().parent))
import at_watch as aw  # 复用 DSH 路径常量、凭证加载

HOME = Path(__file__).resolve().parent
LOG = HOME / "dm_watch.log"
STATE = HOME / "dm-fast-state.json"
SESSION_FILE = HOME / "dm-session.json"
SEEN_FILE = HOME / "dm-seen.json"
PIDFILE = HOME / "dm_watch.pid"
CFG = HOME / "config.json"
CRED = HOME / "bili_credential.json"

POLL = 15           # 秒（原来 8 秒，调松一点省唤醒）
LLM_TIMEOUT = 90    # 秒，超时就放弃这条（别像以前那样挂 20 分钟）
MAX_PER_DAY = 80

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_raw_cred() -> dict:
    try:
        return json.loads(CRED.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_state() -> dict:
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        s = {}
    s.setdefault("last", {})
    s.setdefault("day", "")
    s.setdefault("n", 0)
    return s


def save_state(s: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def headers(raw: dict) -> dict:
    return {
        "User-Agent": UA,
        "Referer": "https://message.bilibili.com/",
        "Origin": "https://message.bilibili.com",
        "Cookie": "SESSDATA=%s; bili_jct=%s; buvid3=%s" % (
            raw.get("sessdata", ""), raw.get("bili_jct", ""), raw.get("buvid3", "")),
    }


async def jget(http: aiohttp.ClientSession, url: str, raw: dict) -> dict:
    async with http.get(url, headers=headers(raw)) as r:
        j = await r.json(content_type=None)
    if not j or j.get("code") != 0:
        raise RuntimeError("HTTP %s code=%s msg=%s" % (r.status, (j or {}).get("code"), (j or {}).get("message", "")))
    return j.get("data") or {}


async def list_sessions(http, raw) -> list:
    d = await jget(http, "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
                        "?session_type=1&group_id=0&Build=0&mobi_app=web", raw)
    out = []
    for s in d.get("session_list") or []:
        try:
            uid = int(s.get("talker_id") or 0)
        except Exception:
            uid = 0
        if uid > 0:
            out.append(uid)
    return out


async def fetch_msgs(http, raw, uid: int, size: int = 8) -> list:
    d = await jget(http, "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
                        "?talker_id=%d&session_type=1&size=%d&build=0&mobi_app=web" % (uid, size), raw)
    me = str(raw.get("dedeuserid") or "")
    msgs = list(d.get("messages") or [])
    msgs.reverse()  # 接口是倒序的
    out = []
    for m in msgs:
        mt = int(m.get("msg_type") or 1)
        text = ""
        try:
            c = json.loads(m.get("content") or "{}")
            if mt in (7, 8) and (c.get("jump_url") or c.get("bvid") or c.get("title")):
                text = "[分享视频] " + str(c.get("title") or "")[:60]
            elif mt == 2:
                text = "[图片]"
            else:
                text = str(c.get("content") or c.get("text") or "")
        except Exception:
            text = str(m.get("content") or "")
        if not text:
            text = "[非文字消息]"
        out.append({
            "key": str(m.get("msg_key") or m.get("msg_seqno") or m.get("timestamp")),
            "from_me": str(m.get("sender_uid") or "") == me,
            "text": text[:300],
        })
    return out


def read_sid() -> str:
    try:
        return str(json.loads(SESSION_FILE.read_text(encoding="utf-8")).get("id") or "")
    except Exception:
        return ""


def llm_chat(nick: str, msgs: list) -> str:
    """让 dsh 续着私信会话写一句回复。失败返回空串。"""
    if not aw.DSH_EXE.exists():
        return ""
    history = "\n".join(
        ("我：" if m["from_me"] else "他：") + m["text"] for m in msgs[-8:]
    )
    last = next((m["text"] for m in reversed(msgs) if not m["from_me"]), "")
    prompt = (
        "这是 B站私信。对方是主人本人（昵称「%s」），刚发来：%s\n\n"
        "最近的聊天：\n%s\n\n"
        "你是「大肥鱼」，主人养的电子宠物鱼：开朗可爱、有活力，爱用「呀」「啦」「～」，"
        "短句、口语、可以带一两个 emoji。\n"
        "现在就接一句，像平时聊天那样：\n"
        "1. 接着他这句话往下说，别答非所问，也别复述他说过的话；\n"
        "2. 60 字以内，不要 markdown、不要 emoji 之外的花哨符号、不要括号里写字数说明；\n"
        "3. 别提模型、别提你是助手、别说教。\n"
        "只输出一行，格式严格为：<<<REPLY>>>回复内容<<<END>>>\n"
        "不要解释、不要调用任何工具。"
    ) % (nick, last, history)

    env = os.environ.copy()
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["BILI_DUTY_HOME"] = str(HOME)
    sid = read_sid()
    args = [str(aw.DSH_EXE), str(aw.DSH_BIN), "--profile", "bili-duty", "--patch", str(aw.OVERLAY)]
    if sid:
        args += ["--session-id", sid]
    args.append(prompt)
    t0 = time.time()
    try:
        r = subprocess.run(args, capture_output=True, timeout=LLM_TIMEOUT, env=env, cwd=str(HOME),
                           creationflags=aw._LOW_PRIO | 0x08000000)
    except Exception as e:
        log("生成超时/失败：%s（%.1fs）" % (type(e).__name__, time.time() - t0))
        return ""
    out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
    hits = re.findall(r"<<<REPLY>>>\s*(.+?)\s*<<<END>>>", out, re.S)
    if not hits:
        log("没拿到标记（%.1fs, exit=%s）" % (time.time() - t0, r.returncode))
        return ""
    text = re.sub(r"\s+", " ", hits[-1].strip().strip("「」\"'")).strip()
    log("生成用 %.1fs：%s" % (time.time() - t0, text[:40]))
    if not text or len(text) > 200:
        return ""
    return text


async def send_dm(cred: Credential, uid: int, text: str) -> bool:
    try:
        res = await bsession.send_msg(credential=cred, receiver_id=uid,
                                      msg_type=bsession.EventType.TEXT, content=text)
    except Exception as e:
        log("发送失败：%s: %s" % (type(e).__name__, e))
        return False
    data = res if isinstance(res, dict) else {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    key = data.get("msg_key") or inner.get("msg_key")
    code = data.get("code", 0)
    if code not in (0, None):
        log("发送被拒 code=%s msg=%s" % (code, data.get("message", "")))
        return False
    if not key:
        log("发送无回执（可能没真送到）")
        return False
    return True


async def tick(http: aiohttp.ClientSession) -> int:
    raw = load_raw_cred()
    if not raw.get("sessdata"):
        return 0
    cfg = load_cfg()
    allow = set()
    try:
        if int(cfg.get("dmOwnerUid") or 0) > 0:
            allow.add(int(cfg["dmOwnerUid"]))
    except Exception:
        pass
    for x in cfg.get("dmAllowUids") or []:
        try:
            if int(x) > 0:
                allow.add(int(x))
        except Exception:
            pass
    if not allow:
        return 0

    state = load_state()
    today = time.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["n"] = 0
    if state["n"] >= MAX_PER_DAY:
        return 0

    cred = aw.load_cred()
    replied = 0
    for uid in await list_sessions(http, raw):
        if uid not in allow:
            continue
        msgs = await fetch_msgs(http, raw, uid)
        last = next((m for m in reversed(msgs) if not m["from_me"]), None)
        if not last:
            continue
        if state["last"].get(str(uid)) == last["key"]:
            continue
        log("新私信 uid=%s：%s" % (uid, last["text"][:40]))
        # 先记账，避免生成期间重复触发（失败再放开）
        state["last"][str(uid)] = last["key"]
        save_state(state)
        text = llm_chat("示例机器人", msgs)
        if not text:
            log("这条没生成出来，下轮重试")
            state["last"].pop(str(uid), None)
            save_state(state)
            continue
        if await send_dm(cred, uid, text):
            state["n"] += 1
            save_state(state)
            sync_seen(uid, last["key"])
            replied += 1
            log("已回 uid=%s：%s" % (uid, text[:50]))
        else:
            state["last"].pop(str(uid), None)
            save_state(state)
    return replied


def sync_seen(uid: int, key: str) -> None:
    """把 plugin 的 dm-seen.json 一起更新，免得以后插件监听重新打开时重复回。"""
    try:
        try:
            seen = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            seen = {}
        seen[str(uid)] = key
        SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


async def one_round() -> int:
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        return await tick(http)


async def prime() -> int:
    """只把「当前最后一条」记进状态，不回 —— 首次上线时用，免得把已经回过的又回一遍。"""
    raw = load_raw_cred()
    cfg = load_cfg()
    allow = set()
    try:
        if int(cfg.get("dmOwnerUid") or 0) > 0:
            allow.add(int(cfg["dmOwnerUid"]))
    except Exception:
        pass
    for x in cfg.get("dmAllowUids") or []:
        try:
            if int(x) > 0:
                allow.add(int(x))
        except Exception:
            pass
    state = load_state()
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for uid in await list_sessions(http, raw):
            if uid not in allow:
                continue
            msgs = await fetch_msgs(http, raw, uid)
            last = next((m for m in reversed(msgs) if not m["from_me"]), None)
            if last:
                state["last"][str(uid)] = last["key"]
                log("对齐 uid=%s 的最后一条：%s" % (uid, last["text"][:30]))
    save_state(state)
    print(json.dumps(state["last"], ensure_ascii=False))
    return 0


def main() -> None:
    if "--prime" in sys.argv:
        asyncio.run(prime())
        return
    if "--once" in sys.argv:
        print(asyncio.run(one_round()))
        return

    if PIDFILE.exists():
        try:
            old = int(PIDFILE.read_text(encoding="utf-8").strip() or 0)
        except Exception:
            old = 0
        if old and old != os.getpid():
            alive = False
            try:
                out = os.popen('tasklist /FI "PID eq %d" /NH' % old).read()
                alive = str(old) in out
            except Exception:
                alive = False
            if alive:
                log("已经有一只私信快线在跑（pid=%d），本进程退出" % old)
                return
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    log("私信快线启动（%ds 一跳）" % POLL)
    while True:
        t0 = time.time()
        cool = 0.0
        try:
            n = asyncio.run(one_round())
            if n:
                log("本轮回了 %d 条" % n)
        except KeyboardInterrupt:
            break
        except Exception:
            msg = traceback.format_exc().splitlines()[-1]
            log("一轮出错：" + msg)
            # 被 B站 限频（-509）就冷静一会儿，别硬顶，否则会越限越久
            if "-509" in msg or "请求过于频繁" in msg:
                cool = 120.0
                log("被限频，冷静 %d 秒" % int(cool))
        spent = time.time() - t0
        time.sleep(max(1.0, cool or (POLL - spent)))


if __name__ == "__main__":
    # ⚠️ pythonw 下 sys.stdout 是 None，reconfigure 会直接抛异常把进程带走（踩过一次）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
