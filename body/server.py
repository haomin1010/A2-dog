"""body 技能服务：运行在 PC2 上，接收 brain 的工具调用并驱动机器狗。

    python3 -m body.server                   # 真机（eth0 连 PC1）
    python3 -m body.server --mock            # 假机器人，任何机器都能跑
    python3 -m body.server --check           # 只读自检：连运控、读状态/里程计/音量，不发任何运动指令

安全机制：
- 同一时刻只执行一个工具；stop/cancel 立即打断并 StopMove。
- 看门狗：执行中超过 WATCHDOG_TIMEOUT 秒没收到 brain 的任何消息（心跳），自动打断并 StopMove。
- 连接断开、新连接顶替旧连接，都会打断并 StopMove。
"""
import argparse
import asyncio
import hmac
import logging
import os
import time

from common.protocol import DEFAULT_PORT, WATCHDOG_TIMEOUT, decode, encode
from common.tools import describe, sanitize

from .robot import FSM_NAMES, MockRobot, RealRobot
from .skills import MotionConfig, Skills

log = logging.getLogger("body")


class BodyServer:
    def __init__(self, robot, skills: Skills, token: str = ""):
        self.robot = robot
        self.skills = skills
        self.token = token
        self.writer = None
        self.task = None
        self.current = None
        self.last_rx = time.monotonic()

    # ---------- 发送 ----------
    async def send(self, writer, msg):
        if writer is None or writer.is_closing():
            return
        try:
            writer.write(encode(msg))
            await writer.drain()
        except (ConnectionError, RuntimeError) as e:
            log.warning("发送失败: %s", e)

    def state(self):
        p = self.robot.pose()
        pose = None
        if p is not None:
            pose = {"x": round(p["x"], 3), "y": round(p["y"], 3), "yaw": round(p["yaw"], 3),
                    "mode": FSM_NAMES[p["mode"]] if 0 <= p["mode"] < len(FSM_NAMES) else p["mode"],
                    "age": round(p["age"], 2)}
        return {"posture": self.skills.current_posture(), "busy": self.current, "pose": pose}

    # ---------- 执行 ----------
    def busy(self):
        return self.task is not None and not self.task.done()

    async def abort(self, reason: str):
        """打断当前工具并停止运动。"""
        if not self.busy():
            return
        log.warning("打断 %s：%s", self.current, reason)
        self.task.cancel()
        await asyncio.wait([self.task], timeout=3.0)
        try:
            await asyncio.to_thread(self.robot.stop_move)
        except Exception as e:
            log.error("StopMove 失败: %s", e)

    async def run_tool(self, writer, call_id, name, args):
        self.current = name
        log.info("执行 #%s %s", call_id, describe(name, args))
        t0 = time.monotonic()
        try:
            msg = await getattr(self.skills, name)(**args)
            log.info("完成 #%s %s（%.1fs）", call_id, msg, time.monotonic() - t0)
            await self.send(writer, {"type": "result", "id": call_id, "ok": True, "msg": msg})
        except asyncio.CancelledError:
            await self.send(writer, {"type": "result", "id": call_id, "ok": False, "msg": "被取消"})
        except Exception as e:
            log.error("失败 #%s %s: %s", call_id, name, e)
            await self.send(writer, {"type": "result", "id": call_id, "ok": False, "msg": str(e)})
        finally:
            self.current = None

    async def on_call(self, writer, msg):
        call_id, name = msg.get("id"), msg.get("tool")
        try:
            args = sanitize(name, msg.get("args") or {})
        except ValueError as e:
            await self.send(writer, {"type": "result", "id": call_id, "ok": False, "msg": f"参数错误: {e}"})
            return
        if name == "stop":
            await self.abort("stop 指令")
            await self.run_tool(writer, call_id, name, args)
            return
        if self.busy():
            await self.send(writer, {"type": "result", "id": call_id, "ok": False,
                                     "msg": f"正在执行 {self.current}，请先 cancel"})
            return
        self.task = asyncio.create_task(self.run_tool(writer, call_id, name, args))

    # ---------- 连接 ----------
    async def handshake(self, reader, writer, peer) -> bool:
        """第一条消息必须是 hello；配置了口令（A2_TOKEN）就校验。失败则拒绝，不影响已有连接。"""
        try:
            msg = decode(await asyncio.wait_for(reader.readline(), 3.0))
        except (asyncio.TimeoutError, ValueError, ConnectionError):
            msg = {}
        if msg.get("type") != "hello":
            await self.send(writer, {"type": "denied", "msg": "需要先发 hello"})
        elif self.token and not hmac.compare_digest(str(msg.get("token", "")), self.token):
            await self.send(writer, {"type": "denied", "msg": "口令错误（检查 A2_TOKEN）"})
        else:
            await self.send(writer, {"type": "welcome"})
            return True
        log.warning("拒绝连接 %s", peer)
        writer.close()
        return False

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        if not await self.handshake(reader, writer, peer):
            return
        if self.writer is not None:
            log.warning("新连接 %s 顶替旧连接", peer)
            await self.abort("新连接接入")
            self.writer.close()
        self.writer = writer
        self.last_rx = time.monotonic()
        log.info("brain 已连接: %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                self.last_rx = time.monotonic()
                try:
                    msg = decode(line)
                except ValueError:
                    log.warning("无法解析: %r", line[:200])
                    continue
                t = msg.get("type")
                if t == "ping":
                    await self.send(writer, {"type": "pong", "state": self.state()})
                elif t == "call":
                    await self.on_call(writer, msg)
                elif t == "cancel":
                    await self.abort("brain 取消")
                    await self.send(writer, {"type": "ack", "of": "cancel"})
                elif t == "damp":
                    await self.abort("急停")
                    try:
                        await asyncio.to_thread(self.robot.damp)
                        self.skills.posture = "lying"
                    except Exception as e:
                        log.error("Damp 失败: %s", e)
                    await self.send(writer, {"type": "ack", "of": "damp"})
        except (ConnectionError, asyncio.IncompleteReadError) as e:
            log.warning("连接异常: %s", e)
        finally:
            if self.writer is writer:
                log.info("brain 断开: %s", peer)
                self.writer = None
                await self.abort("连接断开")
            writer.close()

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.1)
            if self.busy() and time.monotonic() - self.last_rx > WATCHDOG_TIMEOUT:
                await self.abort(f"{WATCHDOG_TIMEOUT}s 没收到心跳")


def self_check(robot: RealRobot):
    """只读自检，不发任何运动指令。"""
    print("GetState:", robot.get_state())
    for _ in range(20):
        p = robot.pose()
        if p:
            break
        time.sleep(0.1)
    print("rt/sportmodestate:", p)
    try:
        print("音量:", robot._audio.GetVolume())
    except Exception as e:
        print("音量读取失败:", e)


async def amain(args):
    robot = MockRobot() if args.mock else RealRobot(args.iface)
    if args.check:
        self_check(robot)
        return
    cfg = MotionConfig()
    if args.max_speed:
        cfg.walk_speed = min(cfg.walk_speed, args.max_speed)
        cfg.run_speed = min(cfg.run_speed, args.max_speed)
    if args.mock:
        cfg.stand_wait = 1.0
    token = os.environ.get("A2_TOKEN", "")
    srv = BodyServer(robot, Skills(robot, cfg), token=token)
    server = await asyncio.start_server(srv.handle, args.host, args.port)
    log.info("body 服务已启动 %s:%s（%s，%s）", args.host, args.port,
             "MOCK" if args.mock else f"真机 {args.iface}", "口令已启用" if token else "未设口令")
    if not token and args.host in ("0.0.0.0", "::"):
        log.warning("监听所有网卡且未设口令：同网段任何人都能连上来控制狗，建议设置 A2_TOKEN")
    asyncio.create_task(srv.watchdog())
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="A2 body 技能服务")
    ap.add_argument("--host", default=os.environ.get("A2_BODY_BIND", "0.0.0.0"),
                    help="监听地址，默认所有网卡（可只绑某个网口，如 192.168.124.162）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--iface", default="eth0", help="连 PC1 的网卡（123 网段）")
    ap.add_argument("--mock", action="store_true", help="不连真机，用假机器人")
    ap.add_argument("--check", action="store_true", help="只读自检后退出")
    ap.add_argument("--max-speed", type=float, default=None, help="线速度上限覆盖（m/s）")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
