"""生产回归用的小工具：以「主人号」(3002) 的身份，通过真实 NapCat WS 发消息给机器人号(3001)。

用法：
  python dev_send.py text "你好"
  python dev_send.py file "<本地路径>" "顺手说一句"
  python dev_send.py burst "<文件路径>" "<文本>"      # 先发文件、0.3 秒后发文本（测合批）
"""
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

WS_ME = "ws://127.0.0.1:3002"
TOKEN = "REPLACE_WITH_YOUR_TOKEN"
BOT_UID = 100000000


class Client:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.seq = 0
        self.pending = {}

    async def connect(self):
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(
            self.url, headers={"Authorization": f"Bearer {TOKEN}"}, heartbeat=30)
        asyncio.create_task(self._reader())

    async def _reader(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            fut = self.pending.pop(data.get("echo"), None)
            if fut and not fut.done():
                fut.set_result(data)

    async def call(self, action, params):
        self.seq += 1
        echo = f"x{self.seq}"
        fut = asyncio.get_event_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send_json({"action": action, "params": params, "echo": echo})
        return await asyncio.wait_for(fut, timeout=30)

    async def send(self, segs):
        resp = await self.call("send_private_msg", {"user_id": BOT_UID, "message": segs})
        mid = (resp.get("data") or {}).get("message_id")
        print(f"  已发送 message_id={mid}  retcode={resp.get('retcode')}  status={resp.get('status')}")
        return mid


async def main():
    mode = sys.argv[1]
    c = Client(WS_ME)
    await c.connect()
    if mode == "text":
        await c.send([{"type": "text", "data": {"text": sys.argv[2]}}])
    elif mode == "file":
        uri = Path(sys.argv[2]).resolve().as_uri()
        segs = [{"type": "file", "data": {"file": uri, "name": Path(sys.argv[2]).name}}]
        if len(sys.argv) > 3:
            segs.append({"type": "text", "data": {"text": sys.argv[3]}})
        await c.send(segs)
    elif mode == "burst":
        uri = Path(sys.argv[2]).resolve().as_uri()
        m1 = await c.send([{"type": "file",
                            "data": {"file": uri, "name": Path(sys.argv[2]).name}}])
        await asyncio.sleep(0.3)
        m2 = await c.send([{"type": "text", "data": {"text": sys.argv[3]}}])
        print(f"  两条 mid = {m1}, {m2}")
    await c.session.close()


if __name__ == "__main__":
    asyncio.run(main())
