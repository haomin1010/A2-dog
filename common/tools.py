"""工具定义：唯一的真相来源。

- brain 用它生成大模型的 function calling schema，并在下发前校验参数；
- body 收到调用后再校验一次（双保险），然后执行。

加新工具：在 TOOL_SPECS 里加一项，再在 body/skills.py 里实现同名方法。
"""
from typing import Any, Dict, List

MAX_TEXT_LEN = 30  # speak 文本截断长度

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "stand_up",
        "description": "让机器狗站起来。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "lie_down",
        "description": "让机器狗趴下（先停止运动再卧倒）。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "stop",
        "description": "立即停止当前运动，原地站着。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "move",
        "description": "直线移动一段距离，不改变朝向。forward/backward 是前进/后退，"
                       "left/right 是向左/向右横移。gait=walk 正常走，gait=run 快跑。",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["forward", "backward", "left", "right"]},
                "distance_m": {"type": "number", "minimum": 0.1, "maximum": 5.0,
                               "description": "距离（米），0.1~5"},
                "gait": {"type": "string", "enum": ["walk", "run"]},
            },
            "required": ["direction", "distance_m", "gait"],
        },
    },
    {
        "name": "turn",
        "description": "原地转向。left 逆时针，right 顺时针。",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["left", "right"]},
                "angle_deg": {"type": "number", "minimum": 5, "maximum": 720,
                              "description": "角度（度），5~720"},
            },
            "required": ["direction", "angle_deg"],
        },
    },
    {
        "name": "speak",
        "description": "用机器狗身上的喇叭说一句简短的中文（不超过30字）。",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要说的话，必填，不超过30字"}},
            "required": ["text"],
        },
    },
    {
        "name": "find",
        "description": "原地转圈寻找符合描述的目标（人或物体），找到后转身对准它并说一声。"
                       "只原地转，不会走过去。description 用简短中文描述目标，例如\"穿红衣服的人\"\"椅子\"。",
        "parameters": {
            "type": "object",
            "properties": {"description": {"type": "string", "description": "要找的目标，简短中文"}},
            "required": ["description"],
        },
    },
    {
        "name": "wait",
        "description": "原地等待若干秒。",
        "parameters": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "minimum": 0.5, "maximum": 30}},
            "required": ["seconds"],
        },
    },
]

_SPEC_BY_NAME = {s["name"]: s for s in TOOL_SPECS}
TOOL_NAMES = list(_SPEC_BY_NAME)


def openai_tools() -> List[Dict[str, Any]]:
    """OpenAI 兼容的 tools 参数（通义 DashScope compatible-mode 可直接用）。"""
    return [{"type": "function", "function": s} for s in TOOL_SPECS]


def sanitize(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规整参数：未知工具/缺参数/枚举不对 -> ValueError；数值超范围 -> 夹到边界。"""
    spec = _SPEC_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"未知工具: {name}")
    props = spec["parameters"]["properties"]
    args = dict(args or {})
    out: Dict[str, Any] = {}
    for key in spec["parameters"]["required"]:
        if key not in args:
            raise ValueError(f"{name} 缺少参数 {key}")
    for key, p in props.items():
        if key not in args:
            continue
        v = args[key]
        if p["type"] == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{name}.{key} 不是数字: {v!r}")
            v = min(max(v, p.get("minimum", v)), p.get("maximum", v))
        elif p["type"] == "string":
            v = str(v).strip()
            if "enum" in p and v not in p["enum"]:
                raise ValueError(f"{name}.{key} 取值 {v!r} 不在 {p['enum']}")
            if not v:
                raise ValueError(f"{name}.{key} 不能为空")
            v = v[:MAX_TEXT_LEN]
        out[key] = v
    return out


def describe(name: str, args: Dict[str, Any]) -> str:
    """给人看的一行描述。"""
    if name == "move":
        d = {"forward": "前进", "backward": "后退", "left": "左移", "right": "右移"}[args["direction"]]
        g = "跑" if args.get("gait") == "run" else "走"
        return f"{d} {args['distance_m']:.2f} 米（{g}）"
    if name == "turn":
        return f"{'左' if args['direction'] == 'left' else '右'}转 {args['angle_deg']:.0f}°"
    if name == "speak":
        return f"说「{args['text']}」"
    if name == "find":
        return f"寻找「{args['description']}」"
    if name == "wait":
        return f"等待 {args['seconds']:.1f} 秒"
    return {"stand_up": "站起来", "lie_down": "趴下", "stop": "停止"}.get(name, name)
