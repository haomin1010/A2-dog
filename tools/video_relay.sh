#!/usr/bin/env bash
# 在 PC2 上运行：把狗相机的组播（230.1.1.1:1720，eth0 上收）原样转成单播发给服务器。
# 只在服务器走 WiFi 时需要（WiFi 下组播几乎全丢）；有线直连 123 网段时服务器能直接收组播，不用开。
# 不解码、不重编码，CPU 占用很低；不涉及任何运动控制。
#   tools/video_relay.sh [服务器地址]        默认 192.168.123.215（服务器的 WiFi 地址）
# 服务器端：python -m brain.dashboard --video relay
SERVER=${1:-192.168.123.215}
PORT=${RELAY_PORT:-5600}
echo "转发相机组播 → $SERVER:$PORT （Ctrl-C 停止）"
exec gst-launch-1.0 -q udpsrc address=230.1.1.1 port=1720 multicast-iface=eth0 buffer-size=4194304 \
  ! udpsink host="$SERVER" port="$PORT" sync=false async=false
