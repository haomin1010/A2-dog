# 系统架构

## 总览

```
┌──────────────── 服务器：brain ─────────────────┐            ┌────────────── PC2：body ──────────────┐     ┌PC1┐
│ 麦克风 ─► 录音+VAD ─► qwen3-asr-flash ─► 文本    │            │ body.server（asyncio TCP :8765）       │     │   │
│                                   │             │            │   ├─ 校验参数（common/tools.py）       │     │运 │
│              急停词？──是──► cancel + stop ──────┼──┐         │   ├─ skills：站/趴/走/跑/转/停/说/等 ──┼DDS─►│控 │
│                 │否                              │  │ JSON    │   │    （里程计闭环）                   │     │   │
│                 ▼                                │  │ Lines   │   ├─ 看门狗：1s 无心跳 → StopMove      │     │   │
│  planner：qwen3.5 function calling → 步骤列表    │  ├────────►│   └─ 断线/新连接 → StopMove            │     └───┘
│                 ▼                                │  │ 124网段 │                                        │
│  executor：逐步 call，失败即中止，新指令抢占      │──┘         │ （阶段2）相机组播 → 检测 → 视频回传     │
│  心跳 ping 每 0.3s                               │◄───────────│ （阶段2）自带 SLAM 导航 go_to          │
└─────────────────────────────────────────────────┘            └────────────────────────────────────────┘
```

为什么技能放在 PC2：服务器到不了 PC1；闭环控制（里程计、以后的视觉伺服）要贴着传感器跑；网络断了 PC2 能自己停。

服务器 ↔ PC2 的网络是可选项：brain `--net <名字>`（`brain/config.py` 的 `NETWORKS`）或 `--host <地址>`；
body 默认监听所有网卡。换到共享网络时两端设置相同的 `A2_TOKEN`，握手时校验（见 common/protocol.py）。

## 代码结构

```
common/
  protocol.py   通信协议（JSON Lines）、心跳/看门狗参数
  tools.py      工具定义（唯一真相来源）：大模型 schema + 参数校验 + 人类可读描述
brain/          服务器
  config.py     配置（.env / 环境变量）
  asr.py        arecord 录音 + 能量 VAD + qwen3-asr-flash
  planner.py    一句话 → 工具调用序列（带 3 轮简短历史）
  body_client.py  TCP 客户端 + 心跳
  executor.py   按序执行、失败中止、可取消
  main.py       交互入口
body/           PC2
  robot.py      RealRobot（SDK/DDS）/ MockRobot（假机器人）
  skills.py     技能实现，名字与 tools.py 一一对应
  server.py     技能服务、看门狗、只读自检 --check
tools/
  deploy_pc2.sh   同步 body/common/SDK 到 PC2
  run_body.sh     PC2 上启动 body
  eval_planner.py 规划器回归测试
docs/
```

## 一条指令的生命周期

1. 回车开始录音；能量 VAD 检测说完（静音 0.8s）或再按回车结束。
2. `qwen3-asr-flash` 识别（~0.5s）。
3. 命中急停词（停/别动/站住/不要动）→ **不等大模型**，立刻取消当前任务并 `stop`。
   只是短句就结束；长句（如"停下然后趴下"）继续交给大模型。
4. 其他指令：取消正在进行的任务（新指令优先），调用 `qwen3.5-flash` 一次性规划出全部步骤（~1.2s）。
5. executor 逐步 `call` 给 body，每步等结果；某步失败则中止后续。
6. body 执行技能：Move 以 10Hz 下发，按里程计判断走够距离/转够角度，最后 0.3m / 20° 减速，结束必 StopMove。

## 工具集（阶段 1）

| 工具 | 参数 | 实现 |
|---|---|---|
| stand_up | – | StandUp → 等待 → BalanceStand |
| lie_down | – | StopMove → StandDown |
| stop | – | StopMove（同时打断正在执行的工具） |
| move | direction(forward/backward/left/right)、distance_m(0.1~5)、gait(walk/run) | Move + 里程计闭环；趴着时自动先站起 |
| turn | direction(left/right)、angle_deg(5~720) | Move(0,0,wz) + IMU yaw 闭环 |
| speak | text(≤30字) | AudioClient.TtsMaker |
| wait | seconds(0.5~30) | sleep |

速度上限（body/skills.py `MotionConfig`，保守值，实测后调）：走 0.4 m/s，跑 0.8 m/s，横移 0.25，后退 0.3，转向 0.6 rad/s。
"跑"目前只是更高的速度上限，没有切换步态（SwitchGait 在 A2 上的含义未确认）。

## 安全机制

| 场景 | 处理 | 测试 |
|---|---|---|
| 说"停/别动" | brain 跳过大模型，立即 cancel + stop | mock 已测 |
| 新指令打断旧指令 | 取消旧任务 → body 打断并 StopMove → 执行新计划 | mock 已测 |
| brain 进程崩溃/被杀 | TCP 断开 → body 立即 StopMove | mock 已测 |
| 网络卡死但连接没断 | 1s 收不到心跳 → body 看门狗 StopMove | mock 已测（冻结 brain 进程） |
| 参数离谱（走 100 米） | tools.sanitize 两端都夹到上限 | – |
| 移动被挡住 | 超过预计时间 2 倍 + 3s → 报超时并停 | – |
| 终极急停 | `/damp`（电机阻尼，狗会软倒）；遥控器 | – |

## 路线图

> ⚠ **进入阶段 2 前先评估 [风险 R1：本地算力不足](risks.md)**。
> PC2 和服务器都没有 GPU / 加速卡，下面阶段 2/3 的视觉方案基于未实测的 CPU 估算，可能需要按 R1 的分档方案调整。

- **阶段 1（当前）**：语音/文字 → 大模型 → 站/趴/走/跑/转/停/说/等。
- **阶段 2**：PC2 解码相机组播 → MJPEG 推流到服务器浏览器；接入自带 SLAM：`go_to(地点名)`（先弄清接口）。
- **阶段 3**：YOLO 检测人 + CLIP 匹配描述（"穿红衣服的人"）→ `find(描述)`、`approach(描述)`（视觉伺服，按检测框高度估距离）。
  最终："门外有人" → go_to(门口) → find(穿X衣服的人) → approach → lie_down。

## 待确认

1. 真机上 StandUp / BalanceStand / Move 的实际行为与等待时间（`stand_wait`）。
2. `rt/sportmodestate` 的 position 是否随行走正确累计（决定里程计闭环是否可靠）。
3. SwitchGait / SpeedLevel 在 A2 上的取值。
4. 自带 SLAM 的导航指令接口。
