"""brain 配置。优先读环境变量，其次读项目根目录的 .env（不进 git）。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

LLM_MODEL = os.environ.get("A2_LLM_MODEL", "qwen3.5-flash")   # 想更聪明一点换 qwen3.5-plus
ASR_MODEL = os.environ.get("A2_ASR_MODEL", "qwen3-asr-flash")

# 连 PC2 的网络配置：按名字选（--net / A2_NET），也可以用 --host / A2_BODY_HOST 直接指定地址覆盖。
# 换网络时在这里加一项即可，例如 "wifi": "192.168.x.x"。
NETWORKS = {
    "wired": "192.168.124.162",   # 服务器 USB 网卡直连 PC2 的 net1
    "local": "127.0.0.1",         # 本机 mock 联调
}
DEFAULT_NET = os.environ.get("A2_NET", "wired")
BODY_HOST = os.environ.get("A2_BODY_HOST")                      # 设置了就优先用
BODY_PORT = int(os.environ.get("A2_BODY_PORT", "8765"))
BODY_TOKEN = os.environ.get("A2_TOKEN", "")                     # 可选：和 PC2 上的 A2_TOKEN 一致才能连

MIC_DEVICE = os.environ.get("A2_MIC_DEVICE", "plughw:CARD=Newmine,DEV=0")  # arecord -l 查看

# 命中这些词时不等大模型，立刻停止（之后整句仍会交给大模型，处理"停下然后趴下"这类）
STOP_WORDS = ["停", "别动", "站住", "不要动", "stop"]
