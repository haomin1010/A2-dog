"""意图规划：一句话 -> 按顺序的工具调用列表。每句话只调一次大模型。"""
import json
import logging
import time
from typing import Any, Dict, List, Tuple

from common.tools import describe, openai_tools, sanitize

log = logging.getLogger("planner")

SYSTEM_PROMPT = """你是宇树 A2 四足机器狗的动作规划器。用户用中文口语下达指令，文字来自语音识别，可能有错别字或口误，请按最合理的意思理解。

你的任务：把指令转换成按执行顺序排列的工具调用，一次给出全部步骤。
规则：
1. 只能使用提供的工具，可以一次调用多个，调用顺序就是执行顺序。
2. 机器狗趴着时如果要移动，第一步先 stand_up。
3. 没说距离时默认 1 米；"一点/一小段/一步"约 0.3 米；"一会儿"按 2 米。
4. 用户说 跑/快/冲/赶紧/飞奔 等表示快的词时 gait=run，否则 walk。
5. 掉头/转身/向后转/半圈 = 180°；一圈 = 360°；只说左转/右转 = 90°。
6. "过来"理解为前进；"回去"理解为掉头后前进。
7. 指令跟机器狗动作无关、听不懂、或不安全时，不要做任何动作，只调用 speak 工具简短回复（15 字以内），
   比如让它说"没听清，请再说一遍"。
8. 目前还不会导航到某个地点、也看不到东西（比如"去门口""找人"），遇到这类要求调用 speak 工具
   说明暂时做不到，比如说"我还不会去门口"。调用 speak 时 text 参数必须填写要说的话。
9. 不要输出解释性文字，只调用工具。

机器狗当前状态：{state}"""


class Planner:
    def __init__(self, client, model: str, history_turns: int = 3):
        self.client = client
        self.model = model
        self.history: List[Dict[str, str]] = []
        self.history_turns = history_turns

    def plan(self, text: str, state: str) -> Tuple[List[Tuple[str, Dict[str, Any]]], str, float]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(state=state)}]
        messages += self.history
        messages.append({"role": "user", "content": text})
        t0 = time.monotonic()
        r = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=openai_tools(),
            parallel_tool_calls=True,
            temperature=0.1,
            extra_body={"enable_thinking": False},
        )
        dt = time.monotonic() - t0
        msg = r.choices[0].message
        reply = (msg.content or "").strip()
        steps = []
        for tc in msg.tool_calls or []:
            name = tc.function.name
            try:
                raw = json.loads(tc.function.arguments or "{}")
                if name == "speak" and not raw.get("text") and reply:  # 偶尔模型把话写在正文里
                    raw["text"] = reply
                args = sanitize(name, raw)
            except (ValueError, json.JSONDecodeError) as e:
                log.warning("丢弃无效调用 %s(%s): %s", name, tc.function.arguments, e)
                continue
            steps.append((name, args))

        # 简短历史：让"再走远一点""刚才那样再来一次"这类话能接上
        summary = "；".join(describe(n, a) for n, a in steps) or reply or "（无动作）"
        self.history += [{"role": "user", "content": text},
                         {"role": "assistant", "content": f"已执行：{summary}"}]
        self.history = self.history[-2 * self.history_turns:]
        return steps, reply, dt
