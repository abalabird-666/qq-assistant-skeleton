# 架构说明

## 一条消息的完整路径

```
QQ 服务端
  └─ OneBot 实现（WS）
       └─ qq_watch.Bot.handle_event()       ← 预检：去重 / 白名单 / 空消息
            ├─ 同 uid 合批（1.5s 窗口）      ← 文件+文字 只处理一次
            └─ _flush(uid) → _handle_one()   ← 分派器
                 ├─ _handle_one_v2()         ← 新模式
                 │    ├─ usecase.handle_command()   指令：白名单 / 归档，命中即 return
                 │    └─ _handle_one_legacy()      对话：拼 payload → LLM → 发
                 └─ _handle_one_legacy()            旧模式（默认）
```

## 为什么有「legacy / shadow / new」三档

改这种长期在跑的东西，最怕的是「改完不知道哪里坏了」。所以拆层不是一次性替换，而是三档并跑：

| 模式 | 行为 | 用途 |
|---|---|---|
| `legacy` | 只走旧代码 | 默认值，生产稳定态 |
| `shadow` | 旧代码真正执行，新路由只算不执行，把两者的 payload 的 sha1 写进 `trace_shadow.jsonl` | 上线前对比，`same_as_legacy` 就是验收指标 |
| `new` | 新代码执行 | 逐段切成正式路径 |

切换只改配置 `usecaseMode`，不用改代码。

## use case 契约（`usecase.py`）

```python
class UseCase:
    kind = ""
    def run(self, ctx) -> Ctx: ...
```

约定四条：

1. **只往 `ctx.add()` 里塞内容**，不自己拼 payload —— 拼接顺序由 `plan()` 统一决定；
2. **不抛异常**，失败写进 `ctx.usecase_returns[kind] = {"code": "E_XXX"}`；
3. 只做 IO 的部分才用 `asyncio.to_thread`；
4. 有副作用的登记 `ctx.undo`，发送失败时倒序回滚。

`plan(flags, is_owner)` 返回 `list[Intent]`：同 `mutex_group` 只留 priority 最高的一个，跨组可共存。这条规则是为了防「多意图共存被拆成互斥」。

## 防重复发送（幂等）

```
send_text(uid, text)
  ├─ _is_dup(uid, text)        → 10s 内同文直接跳过；上一条失败时窗口放宽到 120s
  ├─ send_private_msg(timeout=15)
  └─ 失败 → 标记 ok=False + 后台 verify_sent()   ← 回读历史，命中则记 RECOVERED
```

**关键决策：失败后绝不自动重发。** 平台侧的「发送失败」有可能是假失败（消息其实到了），自动重发会变成刷屏。

## trace

每轮对话一个 `trace_id`（一般是最后一条消息的 mid），每次工具调用写一行 JSON：

```json
{"trace": "1623436989", "tool": "qq_archive", "ms": 354, "code": "OK", "ah": "f130f36c70"}
```

`code` 是统一的错误码：`OK / TRUNC / DEGRADED / E_FETCH / E_TIMEOUT / E_UNVERIFIED / SKIP_DUP / NOT_OWNER`。
出现问题时一条 `grep <mid> trace.jsonl` 就能看出断在哪一步。

## 回归（`tests/regress.py`）

22 条用例覆盖：合批、串行、幂等、假失败回读、文件抽字、双上限截断、图片+文件顺序、通读、指令短路、白名单回滚、跨 uid 并发、非主人零泄露。

三条硬约束写在文件头：不改生产配置、不重启生产守护、真发必须带 `[REGRESS]` 前缀。
