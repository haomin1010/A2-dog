# A2-dog：语音控制宇树 A2 机器狗

服务器（brain）听语音、调通义大模型规划动作；PC2（body）执行技能、驱动机器狗。
详见 [docs/architecture.md](docs/architecture.md)，硬件/网络/接口调研见 [docs/system_notes.md](docs/system_notes.md)，
**真机测试步骤（含接线组网）见 [docs/runbook_phase1.md](docs/runbook_phase1.md)**，
风险登记见 [docs/risks.md](docs/risks.md)，真机测试中发现的问题见 [docs/known_issues.md](docs/known_issues.md)（⚠ I1 转向过转，未解决前不要测转向）。

> 整体方案（2026-11-22）：狗和服务器都没有 GPU，**理解上云、安全留本地**——视觉也交给云端多模态模型，PC2 只做基础动作和视频转发。

## 准备

```bash
# 服务器
conda activate a2-dog            # 已装 openai / dashscope / numpy / unitree_sdk2py
echo "DASHSCOPE_API_KEY=sk-..." > .env && chmod 600 .env
```

## 不碰真狗的联调（推荐先做）

```bash
# 终端 1：假机器人
python -m body.server --mock --host 127.0.0.1
# 终端 2
python -m brain.main --net local                 # 回车说话，或直接打字
python tools/eval_planner.py                     # 只测规划，不连 body
```

## 真机

> ⚠ 会让狗真实运动。确认周围空旷、遥控器在手边。

```bash
# 服务器：同步代码到 PC2（PC2 不能上外网，SDK 一起拷）
tools/deploy_pc2.sh

# PC2：先只读自检（不发运动指令），再启动服务
ssh unitree@192.168.124.162
cd ~/lhm/A2-dog
tools/run_body.sh --check
tools/run_body.sh --max-speed 0.3        # 第一次建议限速

# 服务器
python -m brain.main
```

终端指令：回车=说话，直接打字=文字指令，`/stop` 停止，`/damp` 急停（阻尼，狗会软倒），`/state` 状态，`/q` 退出。
说"停/别动/站住"会跳过大模型立即停止。

## 网络（服务器 ↔ PC2）

连接地址是可选项，换网络不用改代码：

- **brain**：`--net wired`（默认，USB 网卡直连 192.168.124.162）/ `--net local`（本机 mock）；默认 `--net dog`（192.168.123.162）；
  新网络在 `brain/config.py` 的 `NETWORKS` 里加一项，或者直接 `--host <PC2 地址>` / 环境变量 `A2_BODY_HOST`。
- **body**：默认监听所有网卡（`0.0.0.0:8765`），可用 `--host` 或 `A2_BODY_BIND` 只绑某个网口。
- **口令（可选，换到共享网络时建议开）**：PC2 和服务器都设置相同的 `A2_TOKEN`，口令不对的连接会被拒绝。
  服务器写进 `.env`；PC2 上 `A2_TOKEN=xxx tools/run_body.sh`。
- **部署脚本**：`PC2=unitree@<地址> tools/deploy_pc2.sh`。

## 演示大屏 / 视频（阶段二）

```bash
# 服务器：只看画面
python -m brain.dashboard                       # 有线直连 123 网段：直接收相机组播
python -m brain.dashboard --video relay         # 走 WiFi：先在 PC2 上运行 tools/video_relay.sh
# 服务器：大模型控制 + 大屏（显示识别原话、规划步骤、执行结果）
python -m brain.main --dashboard [--video relay]

# 自己电脑上看：端口转发后浏览器打开 http://localhost:8080
ssh -J jd_10002 -p 2222 -L 8080:localhost:8080 wanxi@localhost
```

- 截图建测试集：`python tools/capture_frames.py --every 2 --count 50`（存到 `data/frames/`，不进 git）。
- 视觉接口 `brain/vision.py`：`look(图, 问题)` / `locate(图, 描述)`，A 线任务说明见 [docs/phase2_vision_task.md](docs/phase2_vision_task.md)。

## 配置（环境变量或 .env）

| 变量 | 默认 |
|---|---|
| `A2_LLM_MODEL` | `qwen3.5-flash`（更准可用 `qwen3.5-plus`，慢约 0.8s） |
| `A2_ASR_MODEL` | `qwen3-asr-flash` |
| `A2_NET` / `A2_BODY_HOST` / `A2_BODY_PORT` | `wired` / 无（覆盖 NET）/ `8765` |
| `A2_TOKEN` | 空（不校验） |
| `A2_MIC_DEVICE` | `plughw:CARD=Newmine,DEV=0` |

速度上限在 `body/skills.py` 的 `MotionConfig`。

## 加一个新工具

1. `common/tools.py` 的 `TOOL_SPECS` 加定义（名字、描述、参数）。
2. `body/skills.py` 的 `Skills` 加同名 async 方法。
3. 必要时在 `brain/planner.py` 的提示词里加使用规则，跑 `tools/eval_planner.py` 回归。
