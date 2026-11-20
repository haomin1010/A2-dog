"""brain 侧的 body 客户端：TCP JSON Lines，带心跳。"""
import asyncio
import itertools
import logging

from common.protocol import HEARTBEAT_INTERVAL, decode, encode

log = logging.getLogger("body_client")


class BodyClient:
    def __init__(self, host: str, port: int, token: str = ""):
        self.host, self.port, self.token = host, port, token
        self.reader = self.writer = None
        self.state = {}
        self._ids = itertools.count(1)
        self._pending = {}
        self._acks = {}
        self._tasks = []

    @property
    def connected(self):
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self, timeout: float = 3.0):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout)
        # 握手：第一条消息必须是 hello（body 配了 A2_TOKEN 时会校验口令）
        await self._send({"type": "hello", "token": self.token})
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        reply = decode(line) if line else {"type": "denied", "msg": "连接被关闭"}
        if reply.get("type") != "welcome":
            self.writer.close()
            self.writer = None
            raise PermissionError(reply.get("msg", "握手失败"))
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._heartbeat())]

    async def close(self):
        for t in self._tasks:
            t.cancel()
        if self.writer:
            self.writer.close()
        self.writer = None

    async def _send(self, msg):
        if not self.connected:
            raise ConnectionError("未连接 body")
        self.writer.write(encode(msg))
        await self.writer.drain()

    async def _heartbeat(self):
        try:
            while self.connected:
                await self._send({"type": "ping"})
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def _read_loop(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                msg = decode(line)
                t = msg.get("type")
                if t == "pong":
                    self.state = msg.get("state") or {}
                elif t == "result":
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result((bool(msg.get("ok")), msg.get("msg", "")))
                elif t == "ack":
                    fut = self._acks.pop(msg.get("of"), None)
                    if fut and not fut.done():
                        fut.set_result(True)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            log.warning("与 body 的连接已断开")
            if self.writer:
                self.writer.close()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result((False, "与 body 的连接断开"))
            self._pending.clear()

    async def call(self, tool: str, args: dict):
        call_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        await self._send({"type": "call", "id": call_id, "tool": tool, "args": args})
        return await fut

    async def _acked(self, kind: str, timeout: float = 5.0):
        fut = asyncio.get_running_loop().create_future()
        self._acks[kind] = fut
        await self._send({"type": kind})
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            log.warning("%s 没有收到确认", kind)

    async def cancel(self):
        await self._acked("cancel")

    async def damp(self):
        await self._acked("damp")
