"""演示大屏：狗眼实时画面（叠加目标框）+ 识别原话 / 规划步骤 / 执行结果 / 看图结果 + 狗的状态。

单独运行（只看画面）：
    python -m brain.dashboard                    # 有线直连 123 网段，直接收组播
    python -m brain.dashboard --video relay      # 走 WiFi，需要 PC2 上先运行 tools/video_relay.sh
挂在 brain 里（显示完整过程）：
    python -m brain.main --dashboard

在自己电脑上看（经跳板机端口转发）：
    ssh -J jd_10002 -p 2222 -L 8080:localhost:8080 wanxi@localhost
    浏览器打开 http://localhost:8080

默认只监听 127.0.0.1（画面里有人，避免实验室网内其他人直接访问）；需要时 --bind 0.0.0.0。
"""
import argparse
import asyncio
import collections
import json
import logging
import threading
import time

from aiohttp import web

from .video import VideoSource

log = logging.getLogger("dashboard")

KIND_LABEL = {"heard": "听到", "plan": "规划", "step": "执行", "result": "结果", "say": "狗说",
              "vision": "看图", "info": "信息", "error": "错误", "stop": "急停"}


class Dashboard:
    def __init__(self, video: VideoSource = None, stream_width: int = 800, stream_fps: float = 12.0):
        self.video = video
        self.stream_width = stream_width
        self.stream_fps = stream_fps
        self._lock = threading.Lock()
        self._events = collections.deque(maxlen=200)
        self._seq = 0
        self._boxes = []          # [{"box": [x1,y1,x2,y2] 归一化 0~1, "label": str}]
        self._boxes_until = 0.0
        self.body_state = {}
        self.current = ""         # 当前在做什么（大字显示）

    # ---------------- 给 brain 调用的接口（线程安全） ----------------
    def emit(self, kind: str, text: str):
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "t": time.strftime("%H:%M:%S"), "kind": kind,
                                 "label": KIND_LABEL.get(kind, kind), "text": text})

    def set_current(self, text: str):
        self.current = text

    def show_boxes(self, boxes, ttl: float = 4.0):
        """boxes: [{"box": [x1,y1,x2,y2]（0~1 归一化）, "label": "穿红衣服的人"}]，显示 ttl 秒。"""
        with self._lock:
            self._boxes = list(boxes or [])
            self._boxes_until = time.monotonic() + ttl

    # ---------------- 画面 ----------------
    def _frame_jpeg(self):
        import cv2
        f, _ = self.video.latest() if self.video else (None, 0)
        if f is None:
            return None
        h, w = f.shape[:2]
        if self.stream_width and w > self.stream_width:
            f = cv2.resize(f, (self.stream_width, int(h * self.stream_width / w)))
            h, w = f.shape[:2]
        with self._lock:
            boxes = self._boxes if time.monotonic() < self._boxes_until else []
        for b in boxes:
            x1, y1, x2, y2 = b["box"]
            p1, p2 = (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h))
            cv2.rectangle(f, p1, p2, (40, 40, 230), 3)
            label = b.get("label", "")
            if label:  # OpenCV 不支持中文，标签在网页上显示，这里只画序号
                cv2.putText(f, str(boxes.index(b) + 1), (p1[0] + 4, p1[1] + 26),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 40, 230), 2)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return buf.tobytes() if ok else None

    # ---------------- HTTP ----------------
    async def _index(self, request):
        return web.Response(text=PAGE, content_type="text/html")

    async def _stream(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        period = 1.0 / self.stream_fps
        try:
            while True:
                t = time.monotonic()
                jpg = await asyncio.to_thread(self._frame_jpeg)
                if jpg:
                    await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                await asyncio.sleep(max(0.0, period - (time.monotonic() - t)) if jpg else 0.5)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def _snap(self, request):
        jpg = await asyncio.to_thread(self.video.jpeg) if self.video else None
        if not jpg:
            return web.Response(status=503, text="暂无画面")
        return web.Response(body=jpg, content_type="image/jpeg")

    async def _state(self, request):
        since = int(request.query.get("since", 0))
        with self._lock:
            events = [e for e in self._events if e["seq"] > since]
            boxes = self._boxes if time.monotonic() < self._boxes_until else []
        return web.json_response({
            "events": events, "current": self.current, "boxes": boxes,
            "video": self.video.status() if self.video else None, "body": self.body_state,
        }, dumps=lambda o: json.dumps(o, ensure_ascii=False))

    def app(self):
        a = web.Application()
        a.router.add_get("/", self._index)
        a.router.add_get("/stream", self._stream)
        a.router.add_get("/snap.jpg", self._snap)
        a.router.add_get("/api/state", self._state)
        return a

    async def start(self, host: str = "127.0.0.1", port: int = 8080):
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("演示大屏 http://%s:%s", host, port)
        return runner


PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A2 机器狗</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1f2a44;--sub:#6b7280;--red:#c8161e;--line:#e5e7eb}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b2027;--fg:#e6e9ef;--sub:#9aa3b2;--line:#2b313a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:10px 18px;background:var(--red);color:#fff}
header h1{font-size:18px;margin:0}header .st{margin-left:auto;font-size:13px;opacity:.9}
main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:14px;padding:14px}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
#cam{width:100%;border-radius:8px;background:#000;aspect-ratio:16/9;object-fit:contain;display:block}
#current{font-size:22px;font-weight:600;margin:10px 2px 0;min-height:33px}
#boxes{margin:6px 2px 0;color:var(--sub);font-size:14px}
h2{font-size:14px;color:var(--sub);margin:0 0 8px;font-weight:500}
#log{height:calc(100vh - 230px);min-height:300px;overflow-y:auto;font-size:14px}
.ev{padding:6px 0;border-bottom:1px solid var(--line);display:flex;gap:8px}
.ev .t{color:var(--sub);font-size:12px;min-width:58px;padding-top:2px}
.tag{font-size:12px;border-radius:4px;padding:1px 6px;height:20px;white-space:nowrap;color:#fff;background:#6b7280}
.k-heard .tag{background:#4472c4}.k-plan .tag{background:#7c3aed}.k-say .tag{background:#ed7d31}
.k-vision .tag{background:#0f766e}.k-result .tag{background:#70ad47}.k-error .tag,.k-stop .tag{background:var(--red)}
#body{font-size:13px;color:var(--sub);margin-top:10px;word-break:break-all}
</style></head><body>
<header><h1>A2 机器狗 · 演示大屏</h1><span class="st" id="vst">视频：连接中…</span></header>
<main>
 <section class="card">
  <img id="cam" src="/stream" alt="狗眼画面">
  <div id="current"></div><div id="boxes"></div>
 </section>
 <section class="card"><h2>过程</h2><div id="log"></div><div id="body"></div></section>
</main>
<script>
let since=0;const log=document.getElementById('log');
async function tick(){
 try{
  const r=await fetch('/api/state?since='+since);const s=await r.json();
  for(const e of s.events){since=e.seq;const d=document.createElement('div');d.className='ev k-'+e.kind;
   d.innerHTML='<span class="t"></span><span class="tag"></span><span class="x"></span>';
   d.querySelector('.t').textContent=e.t;d.querySelector('.tag').textContent=e.label;d.querySelector('.x').textContent=e.text;
   log.appendChild(d);log.scrollTop=log.scrollHeight;}
  document.getElementById('current').textContent=s.current||'';
  document.getElementById('boxes').textContent=(s.boxes||[]).map((b,i)=>(i+1)+'. '+(b.label||'')).join('　');
  const v=s.video;document.getElementById('vst').textContent=v?('视频：'+(v.frame_age!==null&&v.frame_age<2?v.fps+' 帧/秒':'无画面')):'视频：未开启';
  const b=s.body||{};document.getElementById('body').textContent=b.posture?('狗：'+({standing:'站着',lying:'趴着'}[b.posture]||b.posture)+(b.busy?' · 正在 '+b.busy:'')):'';
 }catch(e){}
 setTimeout(tick,500);}
tick();
document.getElementById('cam').onerror=()=>setTimeout(()=>{document.getElementById('cam').src='/stream?'+Date.now()},2000);
</script></body></html>"""


async def _amain(args):
    video = None if args.video == "off" else VideoSource(args.video, args.iface_addr).start()
    dash = Dashboard(video)
    dash.emit("info", "演示大屏已启动（仅画面模式）")
    await dash.start(args.bind, args.port)
    while True:
        await asyncio.sleep(3600)


def main():
    ap = argparse.ArgumentParser(description="A2 演示大屏（仅画面）")
    ap.add_argument("--video", default="multicast", choices=["multicast", "relay", "off"])
    ap.add_argument("--iface-addr", default="192.168.123.222")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
