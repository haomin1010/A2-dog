"""brain（服务器）<-> body（PC2）通信协议。

TCP 上的 JSON Lines：一行一个 JSON 对象，UTF-8 编码。只用标准库，两端都不需要额外依赖。

握手（连接后第一条消息）
    brain -> body  {"type": "hello", "token": "..."}     token 可为空；body 设置了 A2_TOKEN 时必须一致
    body -> brain  {"type": "welcome"} 或 {"type": "denied", "msg": "..."}（随后断开）

brain -> body
    {"type": "call",   "id": 7, "tool": "move", "args": {...}}   执行一个工具（同一时刻只能有一个在执行）
    {"type": "cancel"}                                          取消正在执行的工具并停止运动
    {"type": "damp"}                                            急停：电机阻尼（狗会软倒）
    {"type": "say", "text": "..."}                              自动播报：不占用工具执行位、不回结果（≤30 字）
    {"type": "ping"}                                            心跳，brain 每 HEARTBEAT_INTERVAL 秒发一次

body -> brain
    {"type": "result", "id": 7, "ok": true,  "msg": "前进了 1.02 米"}
    {"type": "result", "id": 7, "ok": false, "msg": "被取消"}
    {"type": "pong",   "state": {"posture": "standing", "busy": "move", "pose": {...}}}
    {"type": "ack",    "of": "cancel"}
"""
import json

DEFAULT_PORT = 8765
HEARTBEAT_INTERVAL = 0.3  # brain 发心跳的间隔（秒）
WATCHDOG_TIMEOUT = 1.0    # body 超过这么久没收到任何消息，就停掉正在执行的运动


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes) -> dict:
    return json.loads(line.decode("utf-8"))
