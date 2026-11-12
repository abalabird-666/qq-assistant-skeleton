# 个人 QQ / B站 / 微信 自动化助手的骨架

一套跑在自己电脑上的「个人助理」实践：一个 QQ 号当机器人，替你回消息、读文件、做文档、整理群聊摘要；B站号替你刷视频写日报；微信做只读归档。

> **这份仓库只含代码，不含任何账号、登录态、聊天记录或素材。**
> 所有 ID、昵称、群名、路径都已替换成占位符。

## 它解决的不是「怎么调通接口」，而是「调通之后怎么不把自己坑死」

最初的版本能跑，但有一堆只有跑久了才会疼的问题，这个仓库的重点就是它们：

| 问题 | 现象 | 这里的做法 |
|---|---|---|
| 事件并发 | 用户连发「文件 + 一句话」，回两条，第二条没有上下文 | 同用户 1.5s 内合批，按 uid 串行队列 |
| 假失败重发 | 平台报错但其实发到了，重试变成刷屏 | 发送后回读历史确认，失败只标记不重发 |
| 上下文串号 | 两个好友共用一条会话线，聊串 | 每 uid 一个 `session_key`，旧会话回落兼容 |
| 指令被话术带跑 | 「白名单改成…」被人诱导成改配置 | 指令与对话分层，命中即短路，模型 0 次调用 |
| 无超时 | 子进程卡住，整个用户队列堵死 | 60s 硬超时 + kill + 不重试 |
| 改代码靠肉眼 | 每次改完不知道坏了没有 | 22 条回归用例 + shadow 对比（新旧 payload sha1 vs） |

## 目录

```
qq/
  core/     qq_watch.py（主守护）  usecase.py（use case 契约与路由）
  tools/    qq_archive.py  docx_min.py  sticker_lib.py  napcat_alive.py  dev_send.py
  tests/    regress.py（22 条回归） + shadow / selftest 系列
  prompts/  一次性会话的 overlay 配置（禁掉全部工具）
bili/       at_watch.py  dm_watch.py  daily_comment.py  asr.py  favops.py  mcp_server.py
wechat/     wx_archive.py（只读归档）  wx_fetch.py  wx_reader.py（OCR 兜底）
examples/   两个示例配置
docs/       架构说明
```

## 依赖

- Python 3.12（脚本只用标准库 + `aiohttp` / `pypdf` / `Pillow` 这类常见包）
- 一个能和 QQ 通信的 OneBot 实现（本项目按 NapCat 的 WS 协议写，`ws://127.0.0.1:3001`）
- 做文档：纯手写 OOXML，不依赖 python-docx
- 写入库：任意 LLM 的 CLI 或 HTTP 接口（示例里用 `dsh` 的一次性会话）

**仓库不包含、也不提供任何客户端、安装包、账号或登录方式。**

## 配置

```bash
cp examples/config.example.json config.json
# 改 wsUrl / token / masterUins / replyUins
```

`masterUins` 是主人（有全部权限），`replyUins` 是可以自动回复但没有任何权限的朋友。权限门在 `usecase.OWNER_ONLY` 和 `qq_watch.Bot.is_master()` 两处。

## 跑回归

```bash
Q_SELFTEST=1 python qq/tests/regress.py        # 22 条用例，exit code 非 0 即失败
python qq/tests/shadow_round.py                # shadow 对比一轮
```

回归用例默认**不发真实消息**（截获 `send_text`）；要发必须显式 `--live-send` 并加 `[REGRESS]` 前缀。

## 已知不做的

- 不做账号迁移、不做多机部署（就是一个人一台电脑）
- 不保证平台稳定性；被风控踢号是常态，仓库里只有一个掉线看门狗
- `route` / `reply` 两层还没拆完，`R5-收尾.md` 里记了边界条件（未随仓库提供）

## License

MIT。见 [LICENSE](LICENSE)。

## 免责

仅用于个人学习与自动化实践。使用第三方客户端可能违反平台用户协议，账号风险自负。
