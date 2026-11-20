#!/usr/bin/env bash
# 在 PC2 上运行 body 技能服务。额外参数会透传，例如 --check / --mock / --max-speed 0.3
# 只允许在 PC2 上跑真机模式（服务器上只能 --mock）
cd "$(dirname "$0")/.."
if [[ "$(hostname)" != unitree-a2-pc2 && " $* " != *" --mock "* ]]; then
    echo "✗ 当前机器是 $(hostname)，真机 body 只能在 PC2（unitree-a2-pc2）上运行。"
    echo "  先 ssh unitree@192.168.123.162，再 cd ~/lhm/A2-dog && ./tools/run_body.sh $*"
    echo "  （只想在本机联调可以加 --mock）"
    exit 1
fi
exec python3 -u -m body.server "$@"
