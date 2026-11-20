"""机器人硬件抽象层。

RealRobot：通过 unitree_sdk2py 走 DDS 控制 A2（运行在 PC2 上，网卡 eth0 / 192.168.123.x）。
MockRobot：不碰硬件，按下发的速度积分出一个假位姿，用于在任何机器上联调。

所有方法都是同步阻塞的，body/skills.py 会用 asyncio.to_thread 调它们。
"""
import logging
import math
import threading
import time
from typing import Optional

log = logging.getLogger("robot")

# rt/sportmodestate 里 mode 字段的含义（来自 SDK 示例 a2_sport_state.py）
FSM_NAMES = [
    "PASSIVE", "STAND_DOWN", "STAND_UP", "DEFAULT_MODE", "RUNNING_MODE", "CLIMB_MODE",
    "LEFT_SIDE_GAIT", "RIGHT_SIDE_GAIT", "HANDSTAND", "BIPED_STAND", "FRONT_FLIP",
    "BACK_FLIP", "RECOVERY", "BASE_HEIGHT_CTRL",
]
LYING_MODES = {0, 1}  # PASSIVE（阻尼）/ STAND_DOWN（趴下）


class RobotError(RuntimeError):
    pass


def quat_to_yaw(q) -> float:
    """四元数 [w, x, y, z]（宇树 IMUState 的顺序）-> 偏航角（弧度）。"""
    w, x, y, z = (float(v) for v in q)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class RobotBase:
    """位姿 pose() 返回 {"x","y","yaw","mode","age"}，age 是距上次更新的秒数；没有数据返回 None。"""

    def stand_up(self): raise NotImplementedError
    def balance_stand(self): raise NotImplementedError
    def lie_down(self): raise NotImplementedError
    def stop_move(self): raise NotImplementedError
    def damp(self): raise NotImplementedError
    def move(self, vx: float, vy: float, wz: float): raise NotImplementedError
    def speak(self, text: str): raise NotImplementedError
    def pose(self) -> Optional[dict]: raise NotImplementedError
    def close(self): pass


class RealRobot(RobotBase):
    def __init__(self, iface: str = "eth0"):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.a2.sport.sport_client import SportClient
        from unitree_sdk2py.a2.audio.audio_client import AudioClient
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

        ChannelFactoryInitialize(0, iface)

        # 两个 SportClient：速度类指令要快（超时短），姿态类指令（站/趴）可能要等动作执行
        self._fast = SportClient()
        self._fast.SetTimeout(1.0)
        self._fast.Init()
        self._slow = SportClient()
        self._slow.SetTimeout(10.0)
        self._slow.Init()
        self._fast_lock = threading.Lock()
        self._slow_lock = threading.Lock()

        self._audio = AudioClient()
        self._audio.SetTimeout(3.0)
        self._audio.Init()

        self._state_lock = threading.Lock()
        self._pose = None
        self._pose_t = 0.0
        self._sub = ChannelSubscriber("rt/sportmodestate", SportModeState_)
        self._sub.Init(self._on_state, 10)

    # ---- 状态 ----
    def _on_state(self, msg):
        # 注意：A2 的 imu_state.rpy 实测恒为 0（未填充），yaw 必须从四元数 [w,x,y,z] 计算
        p = {"x": float(msg.position[0]), "y": float(msg.position[1]),
             "yaw": quat_to_yaw(msg.imu_state.quaternion), "mode": int(msg.mode)}
        with self._state_lock:
            self._pose, self._pose_t = p, time.monotonic()

    def pose(self) -> Optional[dict]:
        with self._state_lock:
            if self._pose is None:
                return None
            return dict(self._pose, age=time.monotonic() - self._pose_t)

    def get_state(self) -> dict:
        state = {}
        with self._slow_lock:
            code = self._slow.GetState(state)
        if code != 0:
            raise RobotError(f"GetState 失败 code={code}")
        return state

    # ---- 动作 ----
    def _slow_call(self, name, fn, *a):
        with self._slow_lock:
            code = fn(*a)
        if code != 0:
            raise RobotError(f"{name} 失败 code={code}")

    def _fast_call(self, name, fn, *a):
        with self._fast_lock:
            code = fn(*a)
        if code != 0:
            raise RobotError(f"{name} 失败 code={code}")

    def stand_up(self):
        self._slow_call("StandUp", self._slow.StandUp)

    def balance_stand(self):
        self._slow_call("BalanceStand", self._slow.BalanceStand)

    def lie_down(self):
        self._slow_call("StandDown", self._slow.StandDown)

    def damp(self):
        self._fast_call("Damp", self._fast.Damp)

    def stop_move(self):
        self._fast_call("StopMove", self._fast.StopMove)

    def move(self, vx, vy, wz):
        self._fast_call("Move", self._fast.Move, float(vx), float(vy), float(wz))

    def speak(self, text):
        code = self._audio.TtsMaker(text, 0)
        if code != 0:
            raise RobotError(f"TtsMaker 失败 code={code}")


class MockRobot(RobotBase):
    """假机器人：按最近一次 Move 的速度积分位姿，0.5 秒没收到 Move 就当作停下（模拟真狗的超时）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._x = self._y = self._yaw = 0.0
        self._cmd = (0.0, 0.0, 0.0)
        self._cmd_t = 0.0
        self._mode = 1  # 一开始趴着
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        dt = 0.02
        while self._running:
            time.sleep(dt)
            with self._lock:
                if time.monotonic() - self._cmd_t > 0.5:
                    self._cmd = (0.0, 0.0, 0.0)
                vx, vy, wz = self._cmd
                c, s = math.cos(self._yaw), math.sin(self._yaw)
                self._x += (vx * c - vy * s) * dt
                self._y += (vx * s + vy * c) * dt
                self._yaw = math.atan2(math.sin(self._yaw + wz * dt), math.cos(self._yaw + wz * dt))

    def pose(self):
        with self._lock:
            return {"x": self._x, "y": self._y, "yaw": self._yaw, "mode": self._mode, "age": 0.0}

    def stand_up(self):
        log.info("[mock] StandUp"); self._mode = 2

    def balance_stand(self):
        log.info("[mock] BalanceStand"); self._mode = 3

    def lie_down(self):
        log.info("[mock] StandDown"); self._mode = 1

    def damp(self):
        log.info("[mock] Damp"); self._mode = 0

    def stop_move(self):
        with self._lock:
            self._cmd = (0.0, 0.0, 0.0)
        log.info("[mock] StopMove")

    def move(self, vx, vy, wz):
        if self._mode in LYING_MODES:
            raise RobotError("[mock] 趴着时不能 Move")
        with self._lock:
            self._cmd, self._cmd_t = (vx, vy, wz), time.monotonic()

    def speak(self, text):
        log.info("[mock] 说: %s", text)

    def close(self):
        self._running = False
