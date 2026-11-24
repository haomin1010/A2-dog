"""brain 主程序：运行在服务器上。

    python -m brain.main                 # 回车说话 / 直接打字下指令
    python -m brain.main --no-mic        # 只用文字
    python -m brain.main --net local     # 连本机的 mock body
    python -m brain.main --host 10.0.0.8 # 直接指定 PC2 地址（换网络时）

交互：
    回车          开始录音（说完自动结束）
    直接输入文字   当作指令
    /stop        立即停止          /damp   急停（电机阻尼，狗会软倒）
    /state       查看狗的状态      /q      退出
"""
import argparse
import asyncio
import logging
import sys
import threading
import time

from openai import OpenAI

from . import config
from .asr import Recorder, Transcriber
from .body_client import BodyClient
from .executor import Executor
from .planner import Planner
from common.tools import describe

POSTURE_CN = {"standing": "站着", "lying": "趴着", "unknown": "未知"}


def input_thread(loop, queue, recorder, transcriber):
    """阻塞读终端；回车触发录音+识别。结果投递到 asyncio 队列。"""
    def put(item):
        loop.call_soon_threadsafe(queue.put_nowait, item)

    while True:
        line = sys.stdin.readline()
        if not line:
            put(("eof", ""))
            return
        line = line.strip()
        if line:
            put(("text", line))
            continue
        if recorder is None:
            continue
        try:
            wav = recorder.record()
            if wav is None:
                continue
            t0 = time.monotonic()
            text = transcriber.transcribe(wav)
            print(f"🗣  「{text}」（识别 {time.monotonic() - t0:.2f}s）", flush=True)
            if text:
                put(("voice", text))
        except Exception as e:
            print(f"✗ 语音识别失败: {e}", flush=True)


def is_stop(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in config.STOP_WORDS)


async def amain(args):
    if not config.DASHSCOPE_API_KEY:
        sys.exit("没有找到 DASHSCOPE_API_KEY（放到项目根目录 .env 里）")
    client = OpenAI(api_key=config.DASHSCOPE_API_KEY, base_url=config.DASHSCOPE_BASE_URL, timeout=20)
    planner = Planner(client, args.model)
    recorder = None if args.no_mic else Recorder(args.mic)
    transcriber = Transcriber(client, config.ASR_MODEL)

    dash = None
    if args.dashboard:
        from .dashboard import Dashboard
        from .video import VideoSource
        video = None if args.video == "off" else VideoSource(args.video, args.iface_addr).start()
        dash = Dashboard(video)
        await dash.start(args.bind, args.dash_port)
        print(f"演示大屏 http://{args.bind}:{args.dash_port}（视频：{args.video}）")
    report = dash.emit if dash else (lambda kind, text: None)

    body = BodyClient(args.host, args.port, token=config.BODY_TOKEN)
    try:
        await body.connect()
    except PermissionError as e:  # 必须在 OSError 前面（它是 OSError 的子类）
        sys.exit(f"body 拒绝连接：{e}")
    except (OSError, asyncio.TimeoutError) as e:
        sys.exit(f"连不上 body {args.host}:{args.port}（{e}）。PC2 上的 body 服务启动了吗？网络选对了吗（--net/--host）？")
    executor = Executor(body, report=report, set_current=dash.set_current if dash else None)
    if dash:
        async def sync_state():
            while True:
                dash.body_state = dict(body.state or {})
                await asyncio.sleep(0.5)
        asyncio.create_task(sync_state())
        report("info", f"已连接 body {args.host}:{args.port}")
    await asyncio.sleep(0.5)
    print(f"已连接 body {args.host}:{args.port}，狗当前{POSTURE_CN.get(body.state.get('posture'), '状态未知')}")
    print(f"大模型 {args.model} / 语音识别 {config.ASR_MODEL}")
    print("回车=说话  直接打字=文字指令  /stop  /damp  /state  /q" if recorder else
          "打字下指令  /stop  /damp  /state  /q")

    queue = asyncio.Queue()
    threading.Thread(target=input_thread, args=(asyncio.get_running_loop(), queue, recorder, transcriber),
                     daemon=True).start()

    job = None  # 当前指令的"规划+执行"任务；新指令/急停词会把它整个取消

    async def cancel_job():
        nonlocal job
        if job is not None and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await executor.stop()

    async def handle(text):
        posture = POSTURE_CN.get(body.state.get("posture"), "未知")
        try:
            steps, reply, dt = await asyncio.to_thread(planner.plan, text, posture)
        except Exception as e:
            print(f"✗ 大模型调用失败: {e}")
            report("error", f"大模型调用失败: {e}")
            return
        print(f"🧠 规划 {len(steps)} 步（{dt:.2f}s）{('：' + reply) if reply else ''}", flush=True)
        plan_txt = " → ".join(describe(n, a) for n, a in steps) or (reply or "（没有可执行的动作）")
        report("plan", f"{plan_txt}（{dt:.1f}s）")
        if not steps:
            print("  （没有可执行的动作）")
            return
        await executor.start(steps)
        await executor.wait()

    try:
        while True:
            kind, text = await queue.get()
            if kind == "eof" or text in ("/q", "/quit"):
                if job is not None:
                    await asyncio.gather(job, return_exceptions=True)
                break
            if not body.connected:
                print("✗ 与 body 的连接已断开，请重启 brain")
                break
            if text == "/state":
                print(body.state)
                continue
            if text == "/damp":
                await cancel_job()
                await body.damp()
                print("■ 已急停（阻尼）")
                continue
            if kind == "text":
                print(f"⌨  「{text}」")
            report("heard", text)
            if text == "/stop" or is_stop(text):
                # 不等大模型：先停，哪怕上一句还在规划中
                await cancel_job()
                await body.call("stop", {})
                print("■ 已停止", flush=True)
                report("stop", "已停止")
                if text == "/stop" or len(text) <= 4:  # 只是一句"停/别动"，不用再问大模型
                    continue
            await cancel_job()  # 新指令优先
            job = asyncio.create_task(handle(text))
    finally:
        await cancel_job()
        await body.close()


def main():
    ap = argparse.ArgumentParser(description="A2 brain：语音/文字 -> 大模型 -> 机器狗")
    ap.add_argument("--net", default=config.DEFAULT_NET, choices=sorted(config.NETWORKS),
                    help="连 PC2 用哪套网络（见 brain/config.py NETWORKS）")
    ap.add_argument("--host", default=config.BODY_HOST, help="直接指定 body 地址，优先于 --net")
    ap.add_argument("--port", type=int, default=config.BODY_PORT)
    ap.add_argument("--model", default=config.LLM_MODEL)
    ap.add_argument("--mic", default=config.MIC_DEVICE)
    ap.add_argument("--no-mic", action="store_true")
    ap.add_argument("--dashboard", action="store_true", help="同时启动演示大屏")
    ap.add_argument("--video", default="multicast", choices=["multicast", "relay", "off"],
                    help="大屏视频来源：multicast=有线直收组播；relay=WiFi 下由 PC2 转发（tools/video_relay.sh）")
    ap.add_argument("--iface-addr", default="192.168.123.222", help="multicast 模式下服务器在 123 网段的地址")
    ap.add_argument("--bind", default="127.0.0.1", help="大屏监听地址")
    ap.add_argument("--dash-port", type=int, default=8080)
    args = ap.parse_args()
    args.host = args.host or config.NETWORKS[args.net]
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n退出")


if __name__ == "__main__":
    main()
