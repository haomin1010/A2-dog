# A2 系统调研笔记

> 调研时间：2026-11-20。所有结论来自实际登录查看/实测，标注「未确认」的是推断。

## 1. 设备与网络

```
 ┌──────────── 服务器（电脑端 / brain）────────────┐
 │ Ubuntu 22.04，无 GPU，conda 环境 a2-dog（py3.10） │
 │ enp2s0     10.5.2.39       实验室网，能上外网     │
 │ enxec1a…   192.168.124.222 ┐ USB 网卡 AX88179     │
 │            192.168.123.222 ┘ NM 连接名 unitree-a2 │
 │ 麦克风     USB "Newmine"（plughw:CARD=Newmine）   │
 └──────────────────────┬──────────────────────────┘
                        │ 网线（124 网段）
 ┌──────────────────────┴─────── PC2（用户开发 / body）──────────────┐
 │ unitree-a2-pc2，x86 i7-1355U，12 线程，30G 内存，只有核显，不能上外网 │
 │ net1  192.168.124.162（接服务器）   eth0  192.168.123.162（狗内网）  │
 │ ROS2 Humble；系统 python3.10 有 numpy / cyclonedds / cv2 / websockets │
 └──────────────────────────────────┬────────────────────────────────┘
                                    │ 狗内网 123 网段
                          ┌─────────┴─────────┐
                          │ PC1 192.168.123.161 │ 运控（sport / voice 服务、相机）
                          └───────────────────┘
```

要点：
- **服务器只能直连 PC2，ping 不到 PC1**。所以 SDK 控制（DDS）必须跑在 PC2 上；服务器上的 `space_stand.py` 在当前接线下连不上运控。
- 服务器的 `unitree-a2` 连接已设为自动连接（之前 autoconnect=否，插拔后 IP 会丢）。
- PC2 账号 `unitree`。`~/lhm` 是我们的工作目录；`~/A2_Door_Opening`、`~/A2_Turn_Search_Bottle` 属于用户 `a2owner`，不要动。

## 2. 相机（已实测可用）

- A2 标准版：1 个激光雷达 + 1 个高清广角相机（带前灯），相机在 PC1 侧。
- **取流方式：UDP 组播 `230.1.1.1:1720`，RTP/H.264，1280×720**，在 PC2 的 eth0 上能收到。
- SDK 的 a2 模块没有 VideoClient；ROS 里也没有图像话题；PC1 没开 RTSP/HTTP 端口。
- PC2 上可直接解码：

```bash
gst-launch-1.0 udpsrc address=230.1.1.1 port=1720 multicast-iface=eth0 \
  ! application/x-rtp,media=video,encoding-name=H264 ! rtph264depay ! h264parse \
  ! avdec_h264 ! videoconvert ! autovideosink
```

- 画面特点：安装位置低、广角、边缘有暗角；**没有深度**，距离要靠检测框大小估计。

## 3. 运动控制 SDK（unitree_sdk2py.a2）

| 服务 | 主要接口 |
|---|---|
| `sport`（SportClient） | Damp 1001、BalanceStand 1002、StopMove 1003、StandUp 1004、StandDown 1005、RecoveryStand 1006、Euler 1007、**Move(vx,vy,vyaw) 1008**、BodyPosition 1009、SwitchGait 1011、BodyHeight 1013、SpeedLevel 1015、GetState 1034、SetAutoRecovery 1040，以及侧步/倒立/双足/空翻 |
| `voice`（AudioClient） | TtsMaker(text, speaker_id)、Get/SetVolume、LedControl、PlayStream/PlayStop |

- 状态话题：`rt/sportmodestate`（高频）/ `rt/lf/sportmodestate`（低频），类型 `unitree_go SportModeState_`：
  `mode`（FSM 状态，0 PASSIVE / 1 STAND_DOWN / 2 STAND_UP / 3 DEFAULT_MODE / 4 RUNNING_MODE …）、
  `position[3]`、`velocity[3]`、`imu_state.rpy`、`yaw_speed`、`range_obstacle` 等。
- ROS2 侧同样有 `/sportmodestate`、`/dog_odom`（nav_msgs/Odometry）、`/api/sport/request`。
- 未确认：`SwitchGait` / `SpeedLevel` 在 A2 上的取值含义（Go2 注释：0 idle / 1 trot / 2 trot running …）；
  StandUp 后是否必须 BalanceStand 才能 Move（代码里按"需要"处理，实测后再定）。

## 4. 自带 SLAM / 导航服务（PC2，宇树 slam_nav 1.0.0.29）

已确认：
- 进程：`unitree_slam`、禾赛雷达驱动、`point_cloud_fusion`、`qx_arc_driver`（RTK）、`advanced_slam/http_api_server.py`（:8081）。
- **指令入口**：ROS2/DDS 话题 `/api/slam_operate/request`（`unitree_api/msg/Request`，和 sport 同一套请求-响应格式）。
- **状态输出**：`/slam_info`（JSON 字符串），type 有 `pos_info`（导航中位姿/地图）、`ctrl_info`（目标点、是否到达、进度、状态机）、`robot_data`（电机温度、电量、PC2 负载）。
- **HTTP :8081 `/api/operate`** 只做管理：地图文件、栅格地图（1150~1156）、拓扑图点边（1201~1210）、巡检路线与定时（1301~1309）、配置备份（1601~1604）。不直接让狗走。
- 当前状态：`info: "not init"`，`state: "ready"` —— 没加载地图、没在定位。

**现在实际在干什么（2026-11-20 实测）**：开机自启（`launch_slam.sh`），但处于待命，只是在"供数据"：

| 进程 | CPU（单核%） | 实际工作 |
|---|---|---|
| `hesai_ros_driver` | ~15% | 驱动前后两个禾赛雷达 → `/lidar_packets1/2` |
| `point_cloud_fusion` | ~18% | 融合两路雷达 → `/unitree/slam_lidar/points`（10Hz） |
| `unitree_slam` | ~17% | SLAM 主程序，**待命（not init）**，等建图/定位/导航指令 |
| `qx_arc_driver` | ~2% | 千寻 RTK 驱动（户外定位，室内无用） |
| `http_api_server.py` / `http_server` | ~0–1% | 地图、拓扑、巡检的管理接口（给 App 用） |

- 定位输出 `/unitree/slam_relocation/odom`、避障点云 `/collision_clouds` `/safe_clouds`、`/gridmap`、`/unitree_slam/waypoints` 都**没有数据**。
- 结论：SLAM **目前不参与任何控制**，不会给狗发运动指令，也**没有提供避障**。我们的 move/turn 是"盲走"，只靠里程计。
- `/dog_odom`（~1kHz）是运控发出的腿式里程计，跟 SLAM 无关，一直有。
- 总开销约半个核（PC2 共 12 线程），对我们后面跑视觉影响不大；不需要时也可以停掉（`~/graph_pid_ws/StopSlamNav.sh`，未验证）。
- 注意：出厂启动脚本里明文写着 PC2 的 sudo 密码，不要把这类脚本内容外传。

**未确认（不要当成已知）**：
- 开始/结束建图、重定位、导航到某节点的 api_id 和参数格式；
- 导航是"沿拓扑走到节点"还是"任意坐标自动规划"（`ctrl_info` 里有 `targetNodeName`、`isOpenPlan`，倾向前者）；
- 避障/规划默认是否开启。

获取方式（二选一）：宇树文档中心 A2 SLAM 章节原文；或现场用 App 走一遍建图→打点→导航，同时录 `/api/slam_operate/request`。

## 5. 云端模型（阿里云百炼 DashScope，OpenAI 兼容接口）

实测（服务器，2026-11-20）：

| 用途 | 模型 | 延迟 | 备注 |
|---|---|---|---|
| 语音识别 | `qwen3-asr-flash` | ~0.5s | 走 chat.completions + input_audio，选用 |
| 语音识别 | `fun-asr-realtime` / `paraformer-realtime-v2` | 0.5s / 1.35s | 需 dashscope SDK，备选 |
| 规划 | `qwen3.5-flash`（关思考） | ~1.2s | 13 条测试 12 条对，默认 |
| 规划 | `qwen3.5-plus`（关思考） | ~2.0s | 13/13，可切换 |

API key 放在项目根目录 `.env`（`DASHSCOPE_API_KEY=...`，权限 600，已在 .gitignore）。
