#!/usr/bin/env bash
# 在服务器上运行：把 body/ common/ tools/ 和 unitree_sdk2py 同步到 PC2（PC2 不能上外网，SDK 跟着代码一起拷）。
#   tools/deploy_pc2.sh            # 会提示输入 PC2 密码
# 环境变量：PC2（默认 unitree@192.168.123.162）、PC2_DIR（默认 lhm/A2-dog，相对 PC2 的 home）
set -euo pipefail
PC2=${PC2:-unitree@192.168.123.162}
PC2_DIR=${PC2_DIR:-lhm/A2-dog}
cd "$(dirname "$0")/.."

tar czf - --exclude=__pycache__ body common tools -C unitree_sdk2_python unitree_sdk2py \
  | ssh -o PubkeyAuthentication=no "$PC2" "mkdir -p ~/$PC2_DIR && rm -rf ~/$PC2_DIR/body ~/$PC2_DIR/common && tar xzf - -C ~/$PC2_DIR"
echo "已同步到 $PC2:~/$PC2_DIR"
