#!/usr/bin/env bash
# 在 screen 会话 "brain" 里启动 brain（带演示大屏和麦克风）。查看/操作：screen -r brain（退出查看按 Ctrl-A 再按 D）
# 额外参数透传给 brain.main，例如 --video relay / --no-mic
cd "$(dirname "$0")/.."
screen -S brain -X quit 2>/dev/null
screen -dmS brain -L -Logfile logs/brain_screen.log bash -c "~/miniconda3/envs/a2-dog/bin/python -u -m brain.main --dashboard $*; exec bash"
echo "已在 screen 会话 brain 中启动；查看：screen -r brain"
