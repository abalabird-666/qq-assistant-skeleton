# -*- coding: utf-8 -*-
"""R5 第二段：usecase 契约定义（**只有定义，没有业务实现**）。

这一层的目的只有一个：把 `qq_watch.Bot._handle_one` 里那 230 行、45 个子分支，
拆成「一个入口 → 一串 usecase」的形状，并且让以后每一步拆分都能被 R10 回归集验住。

本文件不 import qq_watch（避免循环依赖）：所有 intent 判定仍留在 qq_watch，
调用方把布尔结果打包成 flags 传进来，`plan()` 只做纯粹的「选谁、按什么顺序」。
实现填充留到 R5 第三段。

--------------------------------------------------------------------------
kind → (mutex_group, priority) 映射表
--------------------------------------------------------------------------
priority 越小越先执行；数值就是 `_handle_one` 里原始 if/elif 的先后顺序。

| kind        | 行号(原始) | 触发条件（仓内已有判定函数）        | mutex_group | priority |
|-------------|-----------|-------------------------------------|-------------|----------|
| img         | 1464      | `images_of(ev)` 非空               | img         | 10       |
| file        | 1482      | `files_of(ev)` 非空                | file        | 20       |
| qq_digest   | 1524      | `qq_archive_intent()` == digest     | query       | 30       |
| qq_group    | 1527      | `qq_archive_intent()` == group      | query       | 31       |
| wx_digest   | 1538      | `wx_digest_intent(text)`            | query       | 40       |
| wx_group    | 1555      | `wx_intent(text)`                   | wx_group    | 50       |
| doc_full    | 1565      | `doc_full_intent(text)`             | doc_full    | 60       |
| doc_pages   | 1594      | `doc_pages_intent(text)`            | doc_pages   | 70       |
| make_doc    | 1606      | `file_intent(text)`                 | make_doc    | 80       |

互斥规则（用户裁决）：**只有 `query` 组互斥**——qq_digest / qq_group / wx_digest
三者取 priority 最高的那个，其余组各自独立、可以同时生效（与现状一致）。
`qq_digest` 与 `qq_group` 虽然同组，但同一句话里 `qq_archive_intent` 只会返回一种，
所以它们不会真的互斥（写在同一组只是表达「都在查归档」）。

TODO(R5-3)：doc_full 与 doc_pages 现在也是各干各的（「看完这本」+「继续」会双触发）。
            是否合并成一组，等第三段拆 doc usecase 时一起定，别在这一段偷偷改行为。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import asyncio

# ------------------------------------------------------------------ kind 常量
K_IMG = "img"
K_FILE = "file"
K_QQ_DIGEST = "qq_digest"
K_QQ_GROUP = "qq_group"
K_WX_DIGEST = "wx_digest"
K_WX_GROUP = "wx_group"
K_DOC_FULL = "doc_full"
K_DOC_PAGES = "doc_pages"
K_MAKE_DOC = "make_doc"

#: kind -> (mutex_group, priority)
KIND_META: dict[str, tuple[str, int]] = {
    K_IMG: ("img", 10),
    K_FILE: ("file", 20),
    K_QQ_DIGEST: ("query", 30),
    K_QQ_GROUP: ("query", 31),
    K_WX_DIGEST: ("query", 40),
    K_WX_GROUP: ("wx_group", 50),
    K_DOC_FULL: ("doc_full", 60),
    K_DOC_PAGES: ("doc_pages", 70),
    K_MAKE_DOC: ("make_doc", 80),
}

#: 哪些 kind 只对主人开放（非主人即使命中也不执行）
OWNER_ONLY: frozenset[str] = frozenset({
    K_QQ_DIGEST, K_QQ_GROUP, K_WX_DIGEST, K_WX_GROUP, K_DOC_FULL, K_DOC_PAGES, K_MAKE_DOC,
})


# ------------------------------------------------------------------ Ctx
@dataclass
class Ctx:
    """一次消息处理的全过程上下文。

    只读字段（入口填一次，usecase 不许改）
    ------------------------------------
    trace_id   : str   —— 与 trace.jsonl 里 trace 字段一致（= message_id）
    session_key: str   —— 会话键，形如 "uid:100000001"，R9 之后按 uid 隔离
    uid        : int
    mid        : str   —— 本条消息 id
    text       : str   —— 纯文本部分
    nick       : str
    is_owner   : bool
    mids       : list[str] —— 合批时被合并进来的其它 mid（升序）

    由 usecase 写、下游只读
    -----------------------
    payload_parts : list[str] —— 各 usecase **追加**（append），最后由 compose 统一 join。
                                不许任何 usecase 直接拼 `payload` 字符串，否则顺序就成了隐式契约。
    img_desc      : str  —— img usecase 写
    file_desc     : str  —— file usecase 写
    undo          : list[Callable[[], None]] —— 反向操作（如「白名单被改了要能改回去」），
                    command handler 命中时登记，异常/回滚时倒序执行。
    """

    # --- 只读 ---
    trace_id: str = ""
    session_key: str = ""
    uid: int = 0
    mid: str = ""
    text: str = ""
    nick: str = ""
    is_owner: bool = False
    mids: list[str] = field(default_factory=list)
    # --- 可写 ---
    payload_parts: list[str] = field(default_factory=list)
    img_desc: str = ""
    file_desc: str = ""
    undo: list[Callable[[], None]] = field(default_factory=list)
    # --- 旁路（trace / shadow 用，不参与业务判断）---
    usecase_returns: dict[str, Any] = field(default_factory=dict)
    # --- 依赖注入（本模块不 import qq_watch，凡是要 IO 的都从这里拿）---
    #   parse_groups(text)->list[str] / groups()->list[str] / set_groups(list)->bool
    #   focus_raw()->str / focus_restore(str)->None
    #   archive_intent(text)->tuple|None / archive_run(args)->str
    hooks: dict[str, Callable] = field(default_factory=dict)
    # --- 只判不做（shadow 模式：算出「本该回什么」，但不产生副作用）---
    dry: bool = False

    READONLY = ("trace_id", "session_key", "uid", "mid", "text", "nick", "is_owner", "mids")

    def add(self, piece: str) -> None:
        """usecase 往 payload 追加一段（唯一的合法写法）。"""
        if piece:
            self.payload_parts.append(piece)

    def payload(self, sep: str = "\n\n") -> str:
        """compose 阶段用：把各段拼成最终 prompt。"""
        return sep.join([p for p in self.payload_parts if p])

    def key_for(self, uid: int) -> str:
        return f"uid:{int(uid)}"


# ------------------------------------------------------------------ Intent
@dataclass(frozen=True)
class Intent:
    """一条「该干什么」的判定结果。"""

    kind: str
    mutex_group: str
    priority: int
    args: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def of(kind: str, **args: Any) -> "Intent":
        group, prio = KIND_META[kind]
        return Intent(kind=kind, mutex_group=group, priority=prio, args=dict(args))

    def as_dict(self) -> dict:
        return {"kind": self.kind, "group": self.mutex_group,
                "priority": self.priority, "args": self.args}


# ------------------------------------------------------------------ Plan
def plan(flags: dict[str, bool], is_owner: bool = True,
         args: Optional[dict[str, dict]] = None) -> list[Intent]:
    """router 的**纯函数内核**：flags -> 排序后的执行序列。

    flags : {kind: 是否命中}，由 qq_watch 用现有 intent 函数算好传进来
    args  : {kind: 该 kind 需要的参数}
    返回  : 已按 mutex_group 去重、按 priority 升序排列的 Intent 列表
    """
    args = args or {}
    picked: list[Intent] = []
    for kind, hit in flags.items():
        if not hit or kind not in KIND_META:
            continue
        if kind in OWNER_ONLY and not is_owner:
            continue
        picked.append(Intent.of(kind, **args.get(kind, {})))

    best: dict[str, Intent] = {}
    for it in picked:
        cur = best.get(it.mutex_group)
        if cur is None or it.priority < cur.priority:
            best[it.mutex_group] = it
    return sorted(best.values(), key=lambda i: i.priority)


def route(ctx: Ctx, flags: dict[str, bool], args: Optional[dict[str, dict]] = None) -> list[Intent]:
    """router 的对外签名（第三段接进来）。

    TODO(R5-3)：现在只是 plan() 的薄包装；拆层时这里要负责
                ① 把 ctx 里已有的结果（img_desc/file_desc）变成「已完成」标记，避免重复跑
                ② 记录每个 intent 的判定依据（哪个函数返回了什么），写进 shadow trace
    """
    return plan(flags, is_owner=ctx.is_owner, args=args)


# ------------------------------------------------------------------ command handler
CMD_WHITELIST = "whitelist"
CMD_QQ_ARCHIVE = "qq_archive"
CMD_KINDS = (CMD_WHITELIST, CMD_QQ_ARCHIVE)

# 白名单口令的触发词（与 legacy 的 `_handle_one` 内联判定保持一致，R5 拆完就只剩这一份）
WHITELIST_WORDS = ("白名单", "重点群改成", "重点群改为")


@dataclass
class CommandResult:
    """command handler 的返回值：命中就该 return，不进 compose、不调 LLM。"""

    handled: bool
    reply: str = ""
    kind: str = ""
    undo: list[Callable[[], None]] = field(default_factory=list)


def _cmd_whitelist(ctx: Ctx) -> Optional[CommandResult]:
    """白名单口令：`白名单改成 示例分组B、示例分组C`。"""
    text = ctx.text or ""
    if not text or len(text) >= 120:
        return None
    if not any(w in text for w in WHITELIST_WORDS):
        return None
    parse = ctx.hooks.get("parse_groups")
    setg = ctx.hooks.get("set_groups")
    getg = ctx.hooks.get("groups")
    if not (parse and setg and getg):
        return None

    groups = list(parse(text) or [])
    if ctx.dry:  # shadow：只算不写
        return CommandResult(True, reply=f"[dry] 白名单将改成：{'、'.join(groups) or '（没解析出来）'}",
                             kind=CMD_WHITELIST)

    if not groups:
        return CommandResult(True, reply="没解析出群名，你说「白名单改成 示例分组B、示例分组C」这样就行",
                             kind=CMD_WHITELIST)

    raw_before = ""
    raw_hook = ctx.hooks.get("focus_raw")
    if raw_hook:
        try:
            raw_before = raw_hook() or ""
        except Exception:
            raw_before = ""

    ok = bool(setg(groups))
    if not ok:
        return CommandResult(True, reply="写白名单失败了（focus.json 没写进去），你看一眼文件权限",
                             kind=CMD_WHITELIST)

    undo: list[Callable[[], None]] = []
    restore = ctx.hooks.get("focus_restore")
    if restore:
        def _undo(raw: str = raw_before) -> None:  # 回到改动前的原文
            restore(raw)

        undo.append(_undo)

    now = "、".join(getg() or [])
    ctx.usecase_returns[CMD_WHITELIST] = {"groups": groups, "after": now}
    return CommandResult(True, reply=f"白名单改成：{now}（微信读取那边下一轮生效）",
                         kind=CMD_WHITELIST, undo=undo)


def _cmd_qq_archive(ctx: Ctx) -> Optional[CommandResult]:
    """QQ 归档：`整理一下 QQ 近一个月` / `翻一下 QQ 的示例分组群`。"""
    if not ctx.is_owner:
        return None
    it = ctx.hooks.get("archive_intent")
    run = ctx.hooks.get("archive_run")
    if not (it and run) or not (ctx.text or ""):
        return None

    try:
        got = it(ctx.text)
    except Exception:
        got = None
    if not got:
        return None
    kind, arg = got

    if ctx.dry:  # shadow：不真的起子进程
        return CommandResult(True, reply=f"[dry] 将查 QQ 归档（{kind}:{arg}）", kind=CMD_QQ_ARCHIVE)

    if kind == "digest":
        raw = run(["--digest", str(arg)]) or ""
        head = "QQ"
    else:
        raw = run(["--group", str(arg or ""), "--limit", "30"]) or ""
        head = f"QQ 的「{arg or '最近'}」"

    if raw.startswith("__TIMEOUT__"):
        secs = raw[len("__TIMEOUT__"):] or "60"
        ctx.usecase_returns[CMD_QQ_ARCHIVE] = {"kind": kind, "arg": arg, "code": "E_TIMEOUT"}
        return CommandResult(True, reply=f"归档跑超时了（{secs} 秒就被我掐了），八成是 NapCat 那边堵着。"
                                        f"要不你再喊我一次？我先不重试，免得堵住后面的消息。",
                             kind=CMD_QQ_ARCHIVE)

    if raw and "库里没这个会话" not in raw:
        body = raw if len(raw) <= 3000 else raw[:3000] + "\n……（太长，先截到 3000 字）"
        ctx.usecase_returns[CMD_QQ_ARCHIVE] = {"kind": kind, "arg": arg, "chars": len(raw), "code": "OK"}
        return CommandResult(True, reply=f"{head}近 30 天的记录在这儿：\n\n{body}", kind=CMD_QQ_ARCHIVE)

    # 群名没命中：别把「库里没这个会话」原样当正常回复发出去
    if raw and "库里没这个会话" in raw:
        names: list[str] = []
        lg = ctx.hooks.get("list_groups")
        if lg:
            try:
                names = [str(x) for x in (lg() or [])][:3]
            except Exception:
                names = []
        hint = "、".join(f"「{n}」" for n in names) if names else "（群列表也取不到）"
        ctx.usecase_returns[CMD_QQ_ARCHIVE] = {"kind": kind, "arg": arg, "code": "NOT_FOUND"}
        return CommandResult(
            True,
            reply=f"没找到这个群，库里没有「{arg}」。要不要换成：{hint}？",
            kind=CMD_QQ_ARCHIVE)

    ctx.usecase_returns[CMD_QQ_ARCHIVE] = {"kind": kind, "arg": arg, "chars": 0, "code": "EMPTY"}
    return CommandResult(
        True,
        reply="QQ 归档库还是空的——多半是 QQ 掉线那阵没同步进去，等号在线后我再补。",
        kind=CMD_QQ_ARCHIVE)


def handle_command(ctx: Ctx) -> Optional[CommandResult]:
    """**在 router 之前**拦截「指令类」消息，命中即 return（不进 compose、不调 LLM）。

    为什么要单独一层：这些分支有外部副作用，不该混进「对话素材」里。
    - 白名单口令：改 `focus.json`（共享文件），改完立刻影响微信归档 → 登记 undo
    - QQ 归档：起子进程 `qq_archive.py --ws 3002`，有 IO 和超时 → 超时不重试

    R5-3 已实现这两类；「改配置 / 重启守护 / 清缓存」留到后面挂进来。
    """
    if not ctx.is_owner:
        return None
    for fn in (_cmd_whitelist, _cmd_qq_archive):
        try:
            r = fn(ctx)
        except Exception as e:  # 指令层不许把整条消息搞挂
            ctx.usecase_returns[f"{fn.__name__}_error"] = f"{type(e).__name__}: {e}"
            continue
        if r and r.handled:
            return r
    return None


# ------------------------------------------------------------------ usecase 接口
class UseCase:
    """所有 usecase 的统一形状。

    约定（第三段写实现时逐条遵守）：
    1. `run()` 只做一件事：算出内容，然后 `ctx.add(...)` 追加**一段**。
       不许直接改 `ctx.payload`（没有这个字段），不许自己发消息。
    2. 失败就 return ctx 并留空，**不要抛异常**——异常统一在 compose 外层兜一层，
       否则一条消息里三段失败、用户只看到一段报错，很难查。
    3. 需要 IO 的（下载、抽字、起子进程）才用 `asyncio.to_thread`；
       纯字符串判断（intent）留在 router，不要进 usecase。
    4. 需要撤销的（改了文件/发了文件）往 `ctx.undo` 里登记反向操作。
    """

    kind: str = ""

    def run(self, ctx: Ctx) -> Ctx:  # noqa: D401
        raise NotImplementedError(f"{type(self).__name__}.run 还没实现")

    # 第三段会按 kind 建注册表：
    # REGISTRY: dict[str, UseCase] = {K_IMG: ImageUseCase(), ...}


# ------------------------------------------------------------- FileIngest
class FileIngest(UseCase):
    """B04 / B04a-c / B08：把「收到的文件 → 抽出来的正文」这一段搬出来。

    与 legacy（`_handle_one_legacy` 里 `files = files_of(...)` 那 15 行）逐一对应，
    **不改行为**：

        files = files_of(ev.message)
        for d in files:
            p = await fetch_file(d)              # 失败 continue（B04b）
            one = await to_thread(extract_file_text, p, d.file or p.name)
            if one: chunks.append(one)
        file_desc = "\\n\\n".join(chunks)[:FILE_PAYLOAD_CHARS]

    契约（m06548 已定，不要再改）：
      1. 只写 `ctx.file_desc` 与 `ctx.usecase_returns["file"]`
      2. 失败不抛，code 用 `E_*`：`E_HOOK` / `E_FETCH` / `E_EMPTY`
      3. IO 全走 `ctx.hooks`（`files_of` / `fetch_file` / `extract_file_text`
         / `_find_received_file` / `file_payload_chars`），本模块不 import qq_watch
      4. **不写 LAST_DOC 缓存**：缓存写入留调用方（legacy 现在写在 extract 之后的
         `_load_doc_cache()` 里，将来由 route/doc 那一段负责）
      5. 多文件逐份抽取、按 files 原顺序拼接（B04a）
      6. 没有文件时不写 `usecase_returns`——与 legacy「这段整个不执行」等价
    """

    kind = K_FILE

    async def run(self, ctx: Ctx) -> Ctx:
        get_files = ctx.hooks.get("files_of")
        fetch = ctx.hooks.get("fetch_file")
        extract = ctx.hooks.get("extract_file_text")
        if not (get_files and fetch and extract):
            ctx.usecase_returns[K_FILE] = {"code": "E_HOOK", "chars": 0}
            return ctx

        try:
            files = list(get_files() or [])
        except Exception:
            ctx.usecase_returns[K_FILE] = {"code": "E_HOOK", "chars": 0}
            return ctx
        if not files:                      # 没文件：与 legacy 一样，什么都没发生
            return ctx

        limit_fn = ctx.hooks.get("file_payload_chars")
        try:
            limit = int(limit_fn()) if callable(limit_fn) else 13000
        except Exception:
            limit = 13000

        chunks: list[str] = []
        fetched = 0                        # 真正拿到本地文件的份数
        for d in files:
            try:
                p = await fetch(d)
            except Exception:
                p = None                   # B04b：单份失败跳过，不影响其它文件
            if not p:
                continue
            fetched += 1
            name = str(d.get("file") or getattr(p, "name", ""))
            try:
                one = await asyncio.to_thread(extract, p, name)
            except Exception:
                one = ""
            if one:
                chunks.append(one)

        desc = "\n\n".join(chunks)[:limit]
        ctx.file_desc = desc
        if desc:
            code = "OK"
        elif not fetched:
            code = "E_FETCH"
        else:
            code = "E_EMPTY"
        ctx.usecase_returns[K_FILE] = {
            "code": code, "chars": len(desc), "files": len(files), "fetched": fetched,
            # F-1 收口：本 usecase 自己一行缓存都没写，但 hooks["extract_file_text"]
            # 内部（qq_watch.py:368 `_save_doc_cache`）会把 PDF 分页缓存写进 LAST_DOC。
            # 所以对调用方要说实话：缓存*确实*被写了，只是不在这里写的。
            "cache_written": True,
        }
        ctx.add(desc)                      # 唯一合法写法；compose 由 route 那一段接
        return ctx


FILE_INGEST = FileIngest()

# TODO(R5-5): doc_full 拆出来的时候，给 extract_file_text 加 `no_cache=True` 参数，
#             让「读文件」和「写分页缓存」两件事分开；那时把上面的 cache_written
#             改成按参数回填，而不是现在这个常量 True。


class ImageIngest(UseCase):
    """B03/B03a-c/B07：图片 → 一句话描述（多图用「；」连，超长按 imgDescChars 截）。

    R5-4 从 `_handle_one_legacy` 原样平移，行为逐字保持：
    - 单张失败（取不到图 / 描述为空）跳过，继续下一张；
    - 全部失败 → code=E_IMG，`ctx.img_desc=""`（legacy 那头继续打「图片没识别出来」）；
    - 不写任何缓存文件。
    """

    kind = K_IMG

    async def run(self, ctx: "Ctx") -> "Ctx":
        get_images = ctx.hooks.get("images_of")
        fetch = ctx.hooks.get("fetch_image")
        describe = ctx.hooks.get("describe_image")
        limit_of = ctx.hooks.get("img_desc_chars")
        if not (get_images and fetch and describe):
            ctx.usecase_returns[K_IMG] = {"code": "E_HOOK", "count": 0, "desc_chars": 0}
            return ctx

        try:
            imgs = list(get_images() or [])
        except Exception:
            imgs = []
        if not imgs:
            return ctx

        limit = 300
        if limit_of:
            try:
                limit = int(limit_of())
            except Exception:
                limit = 300

        descs: list[str] = []
        fetched = 0
        for d in imgs:
            try:
                p = await fetch(d)
            except Exception:
                p = None
            if not p:
                continue
            fetched += 1
            try:
                one = await asyncio.to_thread(describe, p)
            except Exception:
                one = ""
            if one:
                descs.append(one)

        desc = "；".join(descs)[:limit]
        ctx.img_desc = desc
        ctx.usecase_returns[K_IMG] = {
            "code": "OK" if desc else "E_IMG",
            "count": len(imgs),
            "desc_chars": len(desc),
        }
        return ctx


# ------------------------------------------------------------- MakeDoc
class MakeDoc(UseCase):
    """B16 / B16a-d：主人说「做一份文件」→ 生成 docx → 发出去。

    与 legacy 一一对应，**不改行为**：
      - 非主人：什么都不做（code=NOT_OWNER），文案留在 legacy
      - dry（shadow）：只判不做，不生成、不发送（code=DRY）
      - 生成失败：code=E_MAKE
      - 同 mid+同名 120s 内已发过：code=DEDUP，不重发也不删文件（F-9）
      - 发送成功：登记 sent_files（code=OK）；发送失败/异常：undo 清理文件（code=E_SEND）

    hooks：make_document / send_file / doc_dedup_key / doc_is_dup /
           doc_mark_sent / doc_cleanup / span
    """

    kind = K_MAKE_DOC

    async def run(self, ctx: "Ctx") -> "Ctx":
        r: dict = {"file_sent": False, "dry": bool(ctx.dry), "out_name": "", "code": ""}
        ctx.usecase_returns[K_MAKE_DOC] = r

        if not ctx.is_owner:
            r["code"] = "NOT_OWNER"
            return ctx
        if ctx.dry:
            r["code"] = "DRY"
            return ctx

        make = ctx.hooks.get("make_document")
        send = ctx.hooks.get("send_file")
        if not (make and send):
            r["code"] = "E_HOOK"
            return ctx

        try:
            made = await asyncio.to_thread(make, ctx.text)
        except Exception:
            made = None
        if not made:
            r["code"] = "E_MAKE"
            return ctx

        r["out_name"] = getattr(made, "name", str(made))
        key_of = ctx.hooks.get("doc_dedup_key")
        is_dup = ctx.hooks.get("doc_is_dup")
        mark = ctx.hooks.get("doc_mark_sent")
        cleanup = ctx.hooks.get("doc_cleanup")
        span = ctx.hooks.get("span") or (lambda *a, **k: None)

        key = key_of(ctx.mid, r["out_name"]) if key_of else ""
        if key and is_dup and is_dup(key):
            span("make_doc", "DEDUP", "DEDUP", 0, name=r["out_name"])
            r["code"] = "DEDUP"
            return ctx

        try:
            ok = await send(ctx.uid, made)
        except Exception:
            ok = False
        if ok:
            if key and mark:
                mark(key)
                save = ctx.hooks.get("doc_save_state")
                if save:
                    save()
            r["file_sent"] = True
            r["code"] = "OK"
        else:
            if cleanup:
                ctx.undo.append(lambda: cleanup(made))
                cleanup(made)              # 立即撤；undo 留着给上层兜底
            r["code"] = "E_SEND"
        return ctx


MAKE_DOC = MakeDoc()


IMAGE_INGEST = ImageIngest()


# ------------------------------------------------------------------ 自检
if __name__ == "__main__":
    # 只验纯函数：互斥、排序、owner 门
    f = {K_QQ_DIGEST: True, K_QQ_GROUP: True, K_WX_DIGEST: True,
         K_WX_GROUP: True, K_MAKE_DOC: True, K_IMG: True, K_FILE: True}
    got = [i.kind for i in plan(f, is_owner=True)]
    assert got == [K_IMG, K_FILE, K_QQ_DIGEST, K_WX_GROUP, K_MAKE_DOC], got
    got2 = [i.kind for i in plan({K_WX_DIGEST: True}, is_owner=False)]
    assert got2 == [], got2
    print("usecase.py 契约自检 OK：", got)
