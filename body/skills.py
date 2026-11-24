"""技能层：每个工具一个 async 方法，名字和 common/tools.py 里的 TOOL_SPECS 对应。

约定：
- 返回一句给人看的结果描述；失败抛异常（RobotError / SkillError）。
- 可以被 asyncio 取消（task.cancel()）；运动类技能在 finally 里一定会 StopMove。
- 位移/转角优先用 rt/sportmodestate 里程计闭环；收不到里程计时退化为按时间估算。
"""
import asyncio
import logging
import math
import time
from dataclasses import dataclass

from .robot import LYING_MODES, RobotBase

log = logging.getLogger("skills")


class SkillError(RuntimeError):
    pass


@dataclass
class MotionConfig:
    # 速度上限（m/s, rad/s）。保守起步，实测后再调大。
    walk_speed: float = 0.4
    run_speed: float = 0.8
    lateral_speed: float = 0.25   # 横移
    backward_speed: float = 0.3   # 后退
    turn_speed: float = 0.6       # 转向角速度 rad/s
    control_dt: float = 0.1       # Move 下发周期（秒）
    stand_wait: float = 3.0       # 站起/趴下动作的等待时间（秒），待实测校准
    odom_max_age: float = 0.5     # 里程计超过这个秒数没更新就视为不可用


def _wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


class Skills:
    def __init__(self, robot: RobotBase, cfg: MotionConfig = None):
        self.robot = robot
        self.cfg = cfg or MotionConfig()
        self.posture = "unknown"  # standing / lying / unknown；有里程计 mode 时以 mode 为准

    async def _io(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    def _fresh_pose(self):
        p = self.robot.pose()
        return p if p and p["age"] < self.cfg.odom_max_age else None

    def current_posture(self) -> str:
        p = self._fresh_pose()
        if p is not None:
            return "lying" if p["mode"] in LYING_MODES else "standing"
        return self.posture

    async def _ensure_standing(self):
        if self.current_posture() != "standing":
            log.info("当前不是站立状态，先自动站起")
            await self.stand_up()
            return
        p = self._fresh_pose()
        if p is not None and p["mode"] == 2:  # STAND_UP（关节锁定站立）还不能走，切到平衡站立
            await self._io(self.robot.balance_stand)
            await asyncio.sleep(0.5)

    # ---------------- 工具实现 ----------------
    async def stand_up(self):
        await self._io(self.robot.stand_up)
        await asyncio.sleep(self.cfg.stand_wait)
        # StandUp 只是把腿撑起来，进入平衡站立后才能接受 Move
        await self._io(self.robot.balance_stand)
        await asyncio.sleep(0.5)
        self.posture = "standing"
        return "已站起"

    async def lie_down(self):
        try:
            await self._io(self.robot.stop_move)
        except Exception as e:  # 趴着时 StopMove 失败无所谓
            log.debug("StopMove: %s", e)
        await self._io(self.robot.lie_down)
        await asyncio.sleep(self.cfg.stand_wait)
        self.posture = "lying"
        return "已趴下"

    async def stop(self):
        await self._io(self.robot.stop_move)
        return "已停止"

    async def move(self, direction: str, distance_m: float, gait: str = "walk"):
        await self._ensure_standing()
        c = self.cfg
        if direction == "forward":
            speed, ux, uy = (c.run_speed if gait == "run" else c.walk_speed), 1.0, 0.0
        elif direction == "backward":
            speed, ux, uy = c.backward_speed, -1.0, 0.0
        elif direction == "left":
            speed, ux, uy = c.lateral_speed, 0.0, 1.0
        else:
            speed, ux, uy = c.lateral_speed, 0.0, -1.0

        start = self._fresh_pose()
        use_odom = start is not None
        t0 = time.monotonic()
        timeout = distance_m / speed * 2.0 + 3.0
        done = 0.0
        try:
            while True:
                now = time.monotonic()
                if use_odom:
                    p = self._fresh_pose()
                    if p is None:
                        raise SkillError("里程计中断")
                    done = math.hypot(p["x"] - start["x"], p["y"] - start["y"])
                else:
                    done = speed * (now - t0)
                remaining = distance_m - done
                if remaining <= 0.03:
                    break
                if now - t0 > timeout:
                    raise SkillError(f"移动超时（只走了 {done:.2f} 米，可能被挡住了）")
                # 最后 0.3 米线性减速，避免冲过头
                scale = 1.0 if remaining > 0.3 else max(0.35, remaining / 0.3)
                v = speed * scale
                await self._io(self.robot.move, ux * v, uy * v, 0.0)
                await asyncio.sleep(c.control_dt)
        finally:
            await self._io(self.robot.stop_move)
        src = "里程计" if use_odom else "按时间估算"
        return f"移动了 {done:.2f} 米（{src}）"

    async def turn(self, direction: str, angle_deg: float):
        await self._ensure_standing()
        c = self.cfg
        target = math.radians(angle_deg)
        sign = 1.0 if direction == "left" else -1.0
        start = self._fresh_pose()
        use_odom = start is not None
        prev_yaw = start["yaw"] if use_odom else 0.0
        t0 = time.monotonic()
        # 保险（见 docs/known_issues.md I1）：超时收紧，读数失效时也不会大幅过转
        timeout = target / c.turn_speed * 1.3 + 1.0
        done = 0.0
        try:
            while True:
                now = time.monotonic()
                if use_odom:
                    p = self._fresh_pose()
                    if p is None:
                        raise SkillError("里程计中断")
                    done += sign * _wrap(p["yaw"] - prev_yaw)
                    prev_yaw = p["yaw"]
                    # 保险：已经发了 1.5 秒转向指令，朝向几乎没变 → 读数不可信，立即停
                    if now - t0 > 1.5 and abs(done) < math.radians(3):
                        raise SkillError("朝向读数不更新（转了 1.5 秒仍不到 3°），已停止")
                    # 保险：转的方向和读数相反 → 符号错了，立即停
                    if done < -math.radians(10):
                        raise SkillError(f"朝向变化方向与指令相反（{math.degrees(done):.0f}°），已停止")
                else:
                    done = c.turn_speed * (now - t0)
                remaining = target - done
                if remaining <= math.radians(2):
                    break
                if now - t0 > timeout:
                    raise SkillError(f"转向超时（只转了 {math.degrees(done):.0f}°）")
                scale = 1.0 if remaining > math.radians(20) else max(0.35, remaining / math.radians(20))
                await self._io(self.robot.move, 0.0, 0.0, sign * c.turn_speed * scale)
                await asyncio.sleep(c.control_dt)
        finally:
            await self._io(self.robot.stop_move)
        return f"转了 {math.degrees(done):.0f}°"

    async def speak(self, text: str):
        await self._io(self.robot.speak, text)
        # TtsMaker 立即返回；粗略按语速等一下，保证"先说再做"的顺序感
        await asyncio.sleep(min(0.5 + 0.22 * len(text), 8.0))
        return f"说了「{text}」"

    async def wait(self, seconds: float):
        await asyncio.sleep(seconds)
        return f"等了 {seconds:.1f} 秒"
