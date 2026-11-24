"""视觉理解接口（阶段二 A 线）。

★ 接口约定（B 线 find / 大屏只依赖这两个函数，改实现不要改签名）：

    look(image, question) -> LookResult
        回答关于画面的问题。例："前面有几个人？""门开着吗？"
    locate(image, description) -> list[Target]
        找出画面里所有符合描述的目标，按置信度从高到低。没找到返回 []。
        例：locate(img, "穿红衣服的人")

    image：BGR ndarray（OpenCV 格式，来自 brain.video.VideoSource）
    Target.box：归一化坐标 [x1, y1, x2, y2]，范围 0~1，左上角为原点

当前是 B 线写的**最简基线**（qwen3.5-flash + 简单提示词 + 宽松 JSON 解析），只为让 find 能先联调。
A 线（学弟）的任务是在这个接口下把它做好：建测试集、对比模型、优化提示词、评测准确率/延迟/费用。
任务说明见 docs/phase2_vision_task.md。
"""
import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

import cv2
import numpy as np
from openai import OpenAI

from . import config

DEFAULT_MODEL = "qwen3.5-flash"
SEND_WIDTH = 960  # 上传前缩放到的宽度（越小越快越省，但小目标会看不清）


@dataclass
class Target:
    box: List[float]          # [x1, y1, x2, y2]，0~1
    label: str = ""
    score: float = 1.0


@dataclass
class LookResult:
    answer: str
    targets: List[Target] = field(default_factory=list)
    latency: float = 0.0
    raw: str = ""


_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.DASHSCOPE_API_KEY, base_url=config.DASHSCOPE_BASE_URL, timeout=30)
    return _client


def _to_data_url(image: np.ndarray) -> str:
    h, w = image.shape[:2]
    if w > SEND_WIDTH:
        image = cv2.resize(image, (SEND_WIDTH, int(h * SEND_WIDTH / w)))
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return "data:image/jpeg;base64," + base64.b64encode(buf).decode()


def _ask(image: np.ndarray, prompt: str, model: str):
    t = time.monotonic()
    r = _get_client().chat.completions.create(
        model=model, temperature=0.1, extra_body={"enable_thinking": False},
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": _to_data_url(image)}},
            {"type": "text", "text": prompt}]}])
    return (r.choices[0].message.content or "").strip(), time.monotonic() - t


def _parse_json(text: str):
    """宽松解析：去掉 ```json 包裹，取第一个 {...} 或 [...]。"""
    text = re.sub(r"```(?:json)?", "", text)
    m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _boxes_from(obj) -> List[Target]:
    """兼容 {"targets":[...]} / {"people":[...]} / [...]；字段 bbox / bbox_2d；坐标 0~1000。"""
    items = obj if isinstance(obj, list) else next(
        (v for v in (obj or {}).values() if isinstance(v, list)), [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        b = it.get("bbox") or it.get("bbox_2d") or it.get("box")
        if not (isinstance(b, list) and len(b) == 4):
            continue
        try:
            x1, y1, x2, y2 = (min(max(float(v) / 1000.0, 0.0), 1.0) for v in b)
        except (TypeError, ValueError):
            continue
        if x2 > x1 and y2 > y1:
            out.append(Target([x1, y1, x2, y2], str(it.get("label") or it.get("desc") or ""),
                              float(it.get("score", 1.0))))
    return out


def look(image: np.ndarray, question: str, model: str = DEFAULT_MODEL) -> LookResult:
    prompt = (f"你是机器狗的眼睛，这是它前方相机的画面（广角、机位很低）。请用一两句中文简短回答：{question}\n"
              "如果回答涉及画面中的具体物体或人，在回答后另起一行输出 JSON："
              '{"targets": [{"label": "名称", "bbox": [x1,y1,x2,y2]}]}，bbox 为 0~1000 的相对坐标。')
    raw, dt = _ask(image, prompt, model)
    answer = re.split(r"```|\{", raw, maxsplit=1)[0].strip() or raw
    return LookResult(answer=answer, targets=_boxes_from(_parse_json(raw)), latency=dt, raw=raw)


def locate(image: np.ndarray, description: str, model: str = DEFAULT_MODEL) -> List[Target]:
    prompt = (f"这是机器狗前方相机的画面（广角、机位很低）。找出画面中所有符合描述的目标：「{description}」。"
              "只输出 JSON，不要其他文字："
              '{"targets": [{"label": "简短描述", "bbox": [x1,y1,x2,y2], "score": 0~1 的置信度}]}，'
              "bbox 为 0~1000 的相对坐标，按置信度从高到低。没有符合的目标就输出 {\"targets\": []}。")
    raw, _ = _ask(image, prompt, model)
    targets = _boxes_from(_parse_json(raw))
    return sorted(targets, key=lambda t: -t.score)


if __name__ == "__main__":  # 用法：python -m brain.vision 图片.jpg "穿红衣服的人"
    import sys
    img = cv2.imread(sys.argv[1])
    t = time.monotonic()
    print("locate:", locate(img, sys.argv[2]), f"{time.monotonic() - t:.2f}s")
    print("look:", look(img, f"画面里有没有{sys.argv[2]}？"))
