#!/usr/bin/env bash
# 在 PC2 上运行 body 技能服务。额外参数会透传，例如 --check / --mock / --max-speed 0.3
cd "$(dirname "$0")/.."
exec python3 -u -m body.server "$@"
