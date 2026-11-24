"""从狗相机定时截图，用来建立视觉测试集（阶段二 A 线）。只读，不控制狗。

    python tools/capture_frames.py --every 2 --count 50            # 每 2 秒一张，共 50 张
    python tools/capture_frames.py --video relay --every 3 --count 30

图片存到 data/frames/<日期时间>/，data/ 已在 .gitignore 里（画面里有人，不进 git）。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from brain.video import VideoSource  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="multicast", choices=["multicast", "relay"])
    ap.add_argument("--iface-addr", default="192.168.123.222")
    ap.add_argument("--every", type=float, default=2.0, help="间隔秒数")
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = Path(args.out or Path(__file__).resolve().parent.parent / "data" / "frames" / time.strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    v = VideoSource(args.video, args.iface_addr).start()
    if v.wait_frame(20) is None:
        sys.exit(f"20 秒内没有收到画面：{v.status()}")
    last_seq = -1
    for i in range(args.count):
        f, seq = v.latest()
        if f is not None and seq != last_seq:
            p = out / f"{i:04d}.jpg"
            cv2.imwrite(str(p), f, [cv2.IMWRITE_JPEG_QUALITY, 92])
            last_seq = seq
            print(f"[{i + 1}/{args.count}] {p}", flush=True)
        time.sleep(args.every)
    print("完成，保存在", out)


if __name__ == "__main__":
    main()
