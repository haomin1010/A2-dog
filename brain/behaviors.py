"""组合行为（在服务器上执行）：截帧 → 云端看图 → 调用 body 的基础动作。

阶段二：find（原地找人 / 找物体，找到后对准）。
阶段三：approach、report 也放在这里。

依赖都通过构造参数注入，方便用假视觉 / 假机器人测试（见 tools/sim_find.py）：
    grab()                         -> (BGR 帧 或 None, 帧序号)
    locate(frame, description)     -> [Target]（brain.vision.locate 的签名）
    body                           -> BodyClient（call / say）
"""
import asyncio
import logging
import time
from dataclasses import dataclass

from common.tools import describe

log = logging.getLogger("behaviors")

LOCAL_TOOLS = {"find"}  # 这些工具由服务器执行，其余交给 body


@dataclass
class FindConfig:
    hfov_deg: float = 110.0      # 相机水平视场角（广角，估计值，待实测标定）
    step_deg: float = 45.0       # 没找到时每次转多少度（小于视场角，相邻两次画面有重叠）
    settle_s: float = 0.6        # 转完后等画面稳定再截帧
    center_tol_deg: float = 6.0  # 目标偏离中心小于这个角度就不再转
    refine: bool = True          # 对准后再看一眼确认


class Behaviors:
    def __init__(self, body, grab, locate, report=None, show_boxes=None, quiet=False, find_cfg=None):
        self.body = body
        self.grab = grab
        self.locate = locate
        self.report = report or (lambda kind, text: None)
        self.show_boxes = show_boxes or (lambda boxes, ttl=4.0: None)
        self.quiet = quiet
        self.cfg = find_cfg or FindConfig()

    # ---------------- 通用 ----------------
    async def announce(self, text: str):
        """狗开口说一句（不占用 body 的执行位），同时显示在大屏上。"""
        self.report("say", text)
        if not self.quiet:
            await self.body.say(text)

    async def run(self, name: str, args: dict):
        """执行一个本地组合行为，返回 (ok, msg)。"""
        try:
            return await getattr(self, name)(**args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("%s 出错", name)
            return False, f"{describe(name, args)}出错：{e}"

    async def _primitive(self, name: str, args: dict):
        ok, msg = await self.body.call(name, args)
        if not ok:
            raise RuntimeError(msg)
        return msg

    async def _fresh_frame(self, timeout: float = 3.0):
        """等画面稳定，取一帧比当前更新的画面（避免拿到转动前缓存的帧）。"""
        await asyncio.sleep(self.cfg.settle_s)
        _, seq0 = self.grab()
        t = time.monotonic()
        while time.monotonic() - t < timeout:
            f, seq = self.grab()
            if f is not None and seq > seq0:
                return f
            await asyncio.sleep(0.05)
        return None

    async def _look_for(self, description: str):
        frame = await self._fresh_frame()
        if frame is None:
            raise RuntimeError("没有视频画面")
        t = time.monotonic()
        targets = await asyncio.to_thread(self.locate, frame, description)
        dt = time.monotonic() - t
        if targets:
            self.show_boxes([{"box": tg.box, "label": tg.label or description} for tg in targets])
            self.report("vision", f"看到 {len(targets)} 个「{description}」（{dt:.1f}s）")
        else:
            self.report("vision", f"没看到「{description}」（{dt:.1f}s）")
        return targets

    def _offset_deg(self, target) -> float:
        """目标中心相对画面中心的水平角度：正 = 在右边。按视场角线性近似（广角边缘会有误差，refine 会修正）。"""
        cx = (target.box[0] + target.box[2]) / 2.0
        return (cx - 0.5) * self.cfg.hfov_deg

    async def _turn(self, deg: float):
        """deg 正 = 向右转。"""
        if abs(deg) < 1:
            return
        await self._primitive("turn", {"direction": "right" if deg > 0 else "left", "angle_deg": abs(deg)})

    # ---------------- find：原地找目标 ----------------
    async def find(self, description: str):
        c = self.cfg
        n_looks = max(1, int(round(360.0 / c.step_deg)))
        await self.announce(f"我找一下{description}")
        turned = 0.0
        for i in range(n_looks):
            targets = await self._look_for(description)
            if targets:
                off = self._offset_deg(targets[0])
                if abs(off) > c.center_tol_deg:
                    await self._turn(off)
                    turned += off
                    if c.refine:  # 转完再看一眼，修正广角边缘的估计误差
                        again = await self._look_for(description)
                        if again:
                            off2 = self._offset_deg(again[0])
                            if abs(off2) > c.center_tol_deg:
                                await self._turn(off2)
                                turned += off2
                await self.announce(f"我看到{description}了")
                return True, f"找到「{description}」，第 {i + 1} 次看到，累计转了 {turned:+.0f}°"
            if i < n_looks - 1:
                await self._turn(-c.step_deg)  # 没看到：向左转一格继续找
                turned -= c.step_deg
        await self.announce(f"转了一圈，没找到{description}")
        return False, f"转了一圈没有找到「{description}」"
