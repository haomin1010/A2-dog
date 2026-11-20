"""按空格让 A2 站起来 / 趴下。
用法: python space_stand.py [本机 123 网段 IP，默认 192.168.123.222]
  空格  站起来（再按一次趴下）
  d     阻尼（电机放松，急停用）
  q     退出（不改变狗当前状态）
"""
import sys
import termios
import tty

import unitree_sdk2py.core.channel as ch
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.a2.sport.sport_client import SportClient

# 服务器连狗网卡上同时有 124.222 和 123.222，DDS 默认会绑到 124.222，PC1 收不到。
# 所以这里直接按地址绑定 123 网段。
addr = sys.argv[1] if len(sys.argv) > 1 else "192.168.123.222"
ch.ChannelConfigHasInterface = ch.ChannelConfigHasInterface.replace(
    'name="$__IF_NAME__$"', 'address="$__IF_NAME__$"')
ChannelFactoryInitialize(0, addr)

client = SportClient()
client.SetTimeout(10.0)
client.Init()

state = {}
code = client.GetState(state)
if code != 0:
    print(f"连不上运控 (GetState code={code})，检查网线/网卡名")
    sys.exit(1)
print(f"已连接，当前状态: {state.get('fsm_name')}")
print("空格=站起/趴下  d=阻尼  q=退出")

standing = False
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
try:
    tty.setcbreak(fd)
    while True:
        k = sys.stdin.read(1)
        if k == " ":
            if not standing:
                print("站起来 ...", "code", client.StandUp())
            else:
                print("趴下 ...", "code", client.StandDown())
            standing = not standing
        elif k == "d":
            print("阻尼", "code", client.Damp())
            standing = False
        elif k == "q":
            break
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
