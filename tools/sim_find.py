"""find 的仿真测试：mock body + 假视觉（按 mock 狗的真实朝向计算目标在画面中的位置）。不需要真机和云端。

    # 终端 A：python -m body.server --mock --host 127.0.0.1 --port 8775
    python tools/sim_find.py --port 8775 --target-deg -120 --true-hfov 95

target-deg：目标在世界坐标中的方位（度，逆时针为正，狗初始朝向 0°）。
true-hfov：假相机的真实视场角，可以和程序假设的 110° 不同，用来检验"再看一眼修正"。
"""
import argparse
import asyncio
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from brain.behaviors import Behaviors, FindConfig  # noqa: E402
from brain.body_client import BodyClient  # noqa: E402
from brain.vision import Target  # noqa: E402


def wrap_deg(a):
    return (a + 180.0) % 360.0 - 180.0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8775)
    ap.add_argument("--target-deg", type=float, default=-120.0)
    ap.add_argument("--true-hfov", type=float, default=95.0)
    ap.add_argument("--absent", action="store_true", help="目标不存在（应转一圈后报告没找到）")
    args = ap.parse_args()

    body = BodyClient("127.0.0.1", args.port)
    await body.connect()
    await asyncio.sleep(0.5)
    seq = [0]

    def grab():
        seq[0] += 1
        return np.zeros((720, 1280, 3), np.uint8), seq[0]

    def yaw_deg():
        return math.degrees(body.state["pose"]["yaw"])

    def fake_locate(img, desc):
        time.sleep(1.0)  # 模拟云端延迟
        if args.absent:
            return []
        rel = wrap_deg(args.target_deg - yaw_deg())  # 正 = 目标在左边
        if abs(rel) > args.true_hfov / 2:
            return []
        cx = 0.5 - rel / args.true_hfov
        return [Target([cx - 0.05, 0.2, cx + 0.05, 0.9], desc)]

    said = []

    async def fake_say(text):
        said.append(text)
    body.say = fake_say

    b = Behaviors(body, grab, fake_locate, report=lambda k, t: print(f"  [{k}] {t}"),
                  find_cfg=FindConfig(hfov_deg=110.0))
    ok, msg = await body.call("stand_up", {})
    t = time.monotonic()
    ok, msg = await b.run("find", {"description": "穿红衣服的人"})
    await asyncio.sleep(0.5)
    err = wrap_deg(args.target_deg - yaw_deg())
    print(f"结果: ok={ok} {msg}")
    print(f"耗时 {time.monotonic() - t:.1f}s；最终朝向 {yaw_deg():.0f}°，目标方位 {args.target_deg:.0f}°，对准误差 {err:+.1f}°")
    print("狗说:", said)
    await body.close()


if __name__ == "__main__":
    asyncio.run(main())
