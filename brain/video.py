"""狗相机视频：接收 RTP/H.264 → 解码 → 保存最新一帧。

相机在 PC1 上以组播 230.1.1.1:1720 发出 RTP/H.264（1280×720）。两种接收方式：

    multicast  服务器在狗的 123 网段且是有线（组播不丢包）：在指定网卡上加入组播直接收
    relay      服务器走 WiFi（组播几乎全丢）：PC2 运行 tools/video_relay.sh，把组播转成单播发到服务器 RELAY_PORT

两种方式都先由本模块的转发线程把 RTP 包转到 127.0.0.1:LOCAL_PORT，再交给 OpenCV（FFmpeg 后端）解码。
这样不依赖系统路由（服务器默认组播路由走实验室网卡）和 GStreamer 的 H.264 插件（服务器没装）。

    python -m brain.video --snap /tmp/a.jpg      # 抓一帧存图
"""
import argparse
import logging
import os
import socket
import threading
import time
from typing import Optional

import numpy as np

log = logging.getLogger("video")

MCAST_GROUP = "230.1.1.1"
MCAST_PORT = 1720
RELAY_PORT = 5600          # relay 模式：PC2 发到服务器的这个端口
LOCAL_PORT = 5610          # 本机转给解码器的端口（FFmpeg 还会占用 +1 做 RTCP，不能和 RELAY_PORT 相邻）
SDP_PATH = "/tmp/a2dog_video.sdp"

_SDP = f"""v=0
o=- 0 0 IN IP4 127.0.0.1
s=A2 camera
c=IN IP4 127.0.0.1
t=0 0
m=video {LOCAL_PORT} RTP/AVP 96
a=rtpmap:96 H264/90000
"""


class VideoSource:
    def __init__(self, mode: str = "multicast", iface_addr: str = "192.168.123.222"):
        assert mode in ("multicast", "relay")
        self.mode = mode
        self.iface_addr = iface_addr
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._frame_t = 0.0
        self._frame_seq = 0
        self.fps = 0.0
        self.packets = 0
        self._running = False

    # ---------- 生命周期 ----------
    def start(self):
        self._running = True
        threading.Thread(target=self._forward_loop, daemon=True, name="video-fwd").start()
        threading.Thread(target=self._decode_loop, daemon=True, name="video-dec").start()
        return self

    def stop(self):
        self._running = False

    # ---------- 读取 ----------
    def latest(self, max_age: float = 1.0):
        """返回 (帧 BGR ndarray 副本, 序号)；超过 max_age 秒没有新帧返回 (None, seq)。"""
        with self._lock:
            if self._frame is None or time.monotonic() - self._frame_t > max_age:
                return None, self._frame_seq
            return self._frame.copy(), self._frame_seq

    def wait_frame(self, timeout: float = 5.0):
        t = time.monotonic()
        while time.monotonic() - t < timeout:
            f, _ = self.latest()
            if f is not None:
                return f
            time.sleep(0.05)
        return None

    def jpeg(self, width: int = 0, quality: int = 85) -> Optional[bytes]:
        import cv2
        f, _ = self.latest()
        if f is None:
            return None
        if width and f.shape[1] > width:
            f = cv2.resize(f, (width, int(f.shape[0] * width / f.shape[1])))
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    def status(self) -> dict:
        with self._lock:
            age = time.monotonic() - self._frame_t if self._frame is not None else None
        return {"mode": self.mode, "fps": round(self.fps, 1), "packets": self.packets,
                "frame_age": round(age, 2) if age is not None else None}

    # ---------- 内部 ----------
    def _forward_loop(self):
        """收 RTP（组播或单播）→ 转发到 127.0.0.1:LOCAL_PORT。"""
        while self._running:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                if self.mode == "multicast":
                    s.bind(("", MCAST_PORT))
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                 socket.inet_aton(MCAST_GROUP) + socket.inet_aton(self.iface_addr))
                    log.info("加入组播 %s:%s（网卡 %s）", MCAST_GROUP, MCAST_PORT, self.iface_addr)
                else:
                    s.bind(("", RELAY_PORT))
                    log.info("等待 PC2 转发，监听 UDP %s", RELAY_PORT)
                s.settimeout(1.0)
                out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                while self._running:
                    try:
                        data = s.recv(65535)
                    except socket.timeout:
                        continue
                    self.packets += 1
                    out.sendto(data, ("127.0.0.1", LOCAL_PORT))
            except OSError as e:
                log.error("视频接收出错：%s，2 秒后重试", e)
                time.sleep(2)

    def _decode_loop(self):
        with open(SDP_PATH, "w") as fp:
            fp.write(_SDP)
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            "protocol_whitelist;file,udp,rtp|fflags;nobuffer|reorder_queue_size;0|buffer_size;4000000")
        import cv2
        while self._running:
            cap = cv2.VideoCapture(SDP_PATH, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                log.warning("解码器打开失败，2 秒后重试")
                time.sleep(2)
                continue
            log.info("解码器已打开")
            n, t_win, fails = 0, time.monotonic(), 0
            while self._running:
                ok, f = cap.read()
                if not ok:
                    fails += 1
                    if fails > 50:
                        log.warning("连续读帧失败，重建解码器")
                        break
                    time.sleep(0.02)
                    continue
                fails = 0
                now = time.monotonic()
                with self._lock:
                    self._frame, self._frame_t = f, now
                    self._frame_seq += 1
                n += 1
                if now - t_win >= 2.0:
                    self.fps, n, t_win = n / (now - t_win), 0, now
            cap.release()


def main():
    ap = argparse.ArgumentParser(description="狗相机视频接收测试")
    ap.add_argument("--mode", default="multicast", choices=["multicast", "relay"])
    ap.add_argument("--iface-addr", default="192.168.123.222", help="multicast 模式下服务器在 123 网段的地址")
    ap.add_argument("--snap", help="抓一帧保存到该路径后退出")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    v = VideoSource(args.mode, args.iface_addr).start()
    f = v.wait_frame(15)
    if f is None:
        raise SystemExit(f"15 秒内没有收到画面：{v.status()}")
    if args.snap:
        import cv2
        time.sleep(1.0)
        cv2.imwrite(args.snap, v.latest()[0])
        print("已保存", args.snap, v.status())
        return
    while True:
        time.sleep(2)
        print(v.status())


if __name__ == "__main__":
    main()
