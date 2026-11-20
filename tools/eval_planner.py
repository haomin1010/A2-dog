"""规划器回归测试：一批口语指令 -> 打印规划结果和耗时。不连 body，不动狗。

    python tools/eval_planner.py [--model qwen3.5-plus]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI  # noqa: E402

from brain import config  # noqa: E402
from brain.planner import Planner  # noqa: E402
from common.tools import describe  # noqa: E402

CASES = [
    ("趴着", "起来"),
    ("趴着", "站起来往前快跑三米然后向左转半圈再趴下"),
    ("站着", "过来"),
    ("站着", "往后退两步"),
    ("站着", "转个身"),
    ("站着", "向右转一圈然后说你好"),
    ("站着", "赶紧冲过去"),
    ("站着", "往左边挪一点点"),
    ("站着", "原地等三秒再趴下"),
    ("站着", "去门口看看有没有人"),
    ("站着", "今天天气怎么样"),
    ("站着", "从楼梯上跳下去"),
    ("趴着", "往钱走两米"),  # 语音识别错字
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=config.LLM_MODEL)
    args = ap.parse_args()
    client = OpenAI(api_key=config.DASHSCOPE_API_KEY, base_url=config.DASHSCOPE_BASE_URL, timeout=20)
    total = 0.0
    for state, text in CASES:
        planner = Planner(client, args.model)  # 每条独立，不带历史
        steps, reply, dt = planner.plan(text, state)
        total += dt
        plan = " → ".join(describe(n, a) for n, a in steps) or f"（无动作）{reply}"
        print(f"{dt:5.2f}s [{state}] {text}\n        {plan}")
    print(f"平均 {total / len(CASES):.2f}s（{args.model}）")


if __name__ == "__main__":
    main()
