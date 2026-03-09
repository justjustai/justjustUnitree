# Go2 EDU Plus — Connection & Setup Plan

## Current State
- **justjustUnitree repo**: Built, 47 files, 3732 lines, 36 tests passing
- **GitHub**: https://github.com/justjustaibhutani/justjustUnitree
- **Go2 WiFi IP**: 192.168.1.17 (STA mode, on home network)
- **Firmware**: v2 (encrypted WebRTC auth on port 9991)
- **Blocker**: WebRTC SDP exchange hangs, SSH not exposed over WiFi, DDS needs Ethernet

---

## Phase 1: Ethernet Setup (One-Time)

### 1.1 Physical Connection
- Plug USB-C Ethernet adapter into Mac
- Plug Ethernet cable from adapter into Go2's rear Ethernet port
- Power on Go2, wait for full boot (~2 min)

### 1.2 Configure Mac Ethernet
```bash
# Find the Ethernet interface name
ifconfig | grep -B2 "status: active"
# Look for something like en5, en6, en7 (not en0 which is WiFi)

# Set static IP on the Ethernet interface (replace en5 with your interface)
sudo ifconfig en5 192.168.123.99 netmask 255.255.255.0 up

# Verify
ping -c 2 192.168.123.18
```

### 1.3 SSH into Jetson
```bash
ssh unitree@192.168.123.18
# Password: 123

# Verify you're on the Jetson
hostname
uname -a
cat /etc/nv_tegra_release  # Should show Jetson info
```

### 1.4 Discover Internal Network
```bash
# From the Jetson, find all devices on internal network
for i in $(seq 1 254); do ping -c 1 -W 1 192.168.123.$i > /dev/null 2>&1 && echo "192.168.123.$i alive"; done

# Check which Jetson board you're on
ip addr show
cat /proc/device-tree/model 2>/dev/null
```

### 1.5 Update SSH Config
Once we know the exact internal IPs, update `~/.ssh/config`:
```
Host unitreejustdog
    HostName 192.168.123.18    # Internal Jetson IP (via Ethernet)
    User unitree
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
```

---

## Phase 2: Test Official SDK (DDS over Ethernet)

### 2.1 Test from Mac
```bash
# Find your Mac's Ethernet interface name
ifconfig | grep -B2 "192.168.123"
# e.g., en5

# Run Go2 sport client
cd ~/Desktop/Bhutani\ Development/unitree/python_sdk
python3 example/go2/high_level/go2_sport_client.py en5

# Interactive menu: type "stand_up", "stand_down", "move forward", etc.
```

### 2.2 Test from Jetson (over SSH)
```bash
ssh unitree@192.168.123.18

# Check if SDK is already installed
python3 -c "import unitree_sdk2py; print('SDK installed')" 2>/dev/null || echo "Need to install"

# If not installed
cd ~
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd unitree_sdk2_python
pip3 install -e .

# Find the internal network interface on the Jetson
ip addr show | grep "192.168.123"
# e.g., eth0 or enp2s0

# Test
python3 example/go2/high_level/go2_sport_client.py eth0
```

### 2.3 Verify All SDK Features
```bash
# Read robot state (battery, IMU, joints)
python3 example/go2/high_level/go2_sport_client.py en5
# Type: list (to see all commands)
# Type: stand_up
# Type: stand_down
# Type: move forward
# Type: stop_move

# Camera feed (needs display)
python3 example/go2/front_camera/camera_opencv.py en5

# Obstacle avoidance
python3 example/obstacles_avoid/obstacles_avoid_switch.py en5

# Volume/LED control
python3 example/vui_client/vui_client_example.py en5
```

---

## Phase 3: Enable WiFi SSH (Port Forwarding)

### 3.1 Find the Internal Router
```bash
# SSH into Jetson first
ssh unitree@192.168.123.18

# Check the default gateway (this is the internal router)
ip route | grep default
# Should show 192.168.123.1 or 192.168.123.100

# Check what's running on the router
curl -s http://192.168.123.1/ 2>/dev/null | head -20
```

### 3.2 Set Up Port Forwarding
```bash
# Option A: iptables on the router/head unit (if we have access)
# Forward external port 22 → internal 192.168.123.18:22
ssh unitree@192.168.123.100  # or wherever the router is
sudo iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 22 -j DNAT --to-destination 192.168.123.18:22
sudo iptables -A FORWARD -p tcp -d 192.168.123.18 --dport 22 -j ACCEPT

# Option B: SSH reverse tunnel from Jetson to Mac (if iptables not available)
# On Jetson:
ssh -R 2222:localhost:22 justbhutani@192.168.1.15 -N &
# Then from Mac:
ssh -p 2222 unitree@localhost

# Option C: Use a different port to avoid conflicts
# Forward external port 2222 → internal 192.168.123.18:22
```

### 3.3 Verify WiFi SSH
```bash
# From Mac (over WiFi, not Ethernet)
ssh unitree@192.168.1.17
# or
ssh unitree@192.168.1.17 -p 2222  # if using alternate port

# Update SSH config once working
# Host unitreejustdog
#     HostName 192.168.1.17
#     Port 2222  # if needed
#     User unitree
```

---

## Phase 4: Fix WebRTC Connection

### 4.1 Debug from Jetson
```bash
# SSH into Jetson
ssh unitree@192.168.123.18

# Check what's listening on port 9991
sudo netstat -tlnp | grep 9991
sudo ss -tlnp | grep 9991

# Check what's listening on port 8081
sudo netstat -tlnp | grep 8081

# Check firewall
sudo iptables -L -n
```

### 4.2 Test WebRTC from Mac (Clean Attempt)
```bash
# Make sure Unitree app is DISCONNECTED
# Make sure no previous Python processes are hanging
pkill -f go2_webrtc 2>/dev/null

# Single clean attempt
cd ~/Desktop/Bhutani\ Development/justjustUnitree
PYTHONPATH=src python3 -c "
import sys
sys.path.insert(0, '/Users/justbhutani/Library/Python/3.9/lib/python/site-packages')
import asyncio, logging
logging.basicConfig(level=logging.DEBUG)
from go2_webrtc_driver.webrtc_driver import Go2WebRTCConnection, WebRTCConnectionMethod

async def test():
    conn = Go2WebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip='192.168.1.17')
    await asyncio.wait_for(conn.connect(), timeout=30)
    print('CONNECTED!')
    await asyncio.sleep(2)

asyncio.run(test())
"
```

### 4.3 If WebRTC Still Fails
```bash
# Check if the Go2's WebRTC service needs restarting (from Jetson via SSH)
ssh unitree@192.168.123.18

# Find the WebRTC/signaling service
ps aux | grep -i webrtc
ps aux | grep -i signal
ps aux | grep 9991

# Check logs
journalctl -u unitree* --no-pager | tail -50
ls /var/log/unitree* 2>/dev/null
find /home/unitree -name "*.log" 2>/dev/null | head -10
```

---

## Phase 5: Deploy justjustUnitree to Jetson

### 5.1 Clone and Install
```bash
ssh unitree@192.168.123.18

# Clone repo
cd ~
git clone https://github.com/justjustaibhutani/justjustUnitree.git
cd justjustUnitree

# Check Python version on Jetson
python3 --version  # Needs 3.11+ for asyncio.TaskGroup

# Install
pip3 install -e .
# or if Python < 3.11:
pip3 install -e ".[dev]"
```

### 5.2 Set Up API Keys
```bash
# Create env file on Jetson
cat > ~/unitree_keys.env << 'EOF'
export ROBOT_ID="go2"
export ROBOT_IP="192.168.123.18"
export AZURE_API_KEY="<from robot_keys.env backup>"
export AZURE_ENDPOINT="bhuta-mht8rp1z-eastus2.cognitiveservices.azure.com"
export AZURE_REALTIME_DEPLOYMENT="gpt-realtime-2025-08-28"
export DASHBOARD_PORT=5003
EOF
```

### 5.3 Run
```bash
# Dashboard only (test without robot control)
python3 -m jjai_go2 --dashboard-only --port 5003

# Full system (with robot control)
python3 -m jjai_go2 --config config/go2.yaml --env ~/unitree_keys.env

# Access dashboard from Mac browser
# http://192.168.123.18:5003/unitreego2  (over Ethernet)
# http://192.168.1.17:5003/unitreego2    (over WiFi, if port forwarded)
```

### 5.4 Quick Deploy Script (from Mac)
```bash
# After code changes on Mac, deploy to Jetson
cd ~/Desktop/Bhutani\ Development/justjustUnitree
bash deploy.sh
# or manually:
rsync -avz --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  ./ unitree@192.168.123.18:~/justjustUnitree/
```

---

## Phase 6: WiFi-Only Mode (No Ethernet Needed)

### 6.1 Prerequisites
- SSH over WiFi working (Phase 3)
- WebRTC connection working (Phase 4)
- justjustUnitree deployed to Jetson (Phase 5)

### 6.2 Architecture
```
Mac (192.168.1.15)
  |
  |── WiFi ──→ Go2 (192.168.1.17)
  |                 |
  |                 ├── SSH (port forwarded) → Jetson
  |                 ├── WebRTC (port 9991)   → Robot control
  |                 └── Dashboard (port 5003) → Browser UI
  |
  └── Browser → http://192.168.1.17:5003/unitreego2
```

### 6.3 Update justjustUnitree Client for Dual Mode
The Go2Robot client should support both connection methods:
- **WebRTC mode** (WiFi): For voice, camera, audio, commands
- **DDS mode** (Ethernet): For low-latency motor control, state reading

```python
# config/go2.yaml
connection_mode: webrtc  # or "dds" when on Ethernet
robot_ip: 192.168.1.17   # WiFi IP for WebRTC
# robot_ip: 192.168.123.18  # Internal IP for DDS
```

### 6.4 Auto-Start on Boot (Systemd)
```bash
# On Jetson, create systemd service
sudo cat > /etc/systemd/system/jjai-go2.service << 'EOF'
[Unit]
Description=JJAI Go2 Robot Platform
After=network.target

[Service]
Type=simple
User=unitree
WorkingDirectory=/home/unitree/justjustUnitree
ExecStart=/usr/bin/python3 -m jjai_go2 --config config/go2.yaml --env /home/unitree/unitree_keys.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable jjai-go2
sudo systemctl start jjai-go2
```

### 6.5 Deploy Script for WiFi
```bash
# Update deploy.sh to use WiFi SSH
rsync -avz --exclude '.git' --exclude '__pycache__' \
  ./ unitree@192.168.1.17:~/justjustUnitree/
ssh unitree@192.168.1.17 "cd ~/justjustUnitree && pip3 install -e . && sudo systemctl restart jjai-go2"
```

---

## Phase 7: Dashboard Integration

### 7.1 Add to dashboard.justjust.ai
- Deploy as new route on existing EC2 dashboard
- OR: Port-forward Go2's dashboard port through Cloudflare tunnel
- OR: Run dashboard on EC2, connect to Go2 via WebRTC from server side

### 7.2 Proxy Config (EC2 nginx)
```nginx
location /unitreego2 {
    proxy_pass http://192.168.1.17:5003;  # Go2 on home WiFi
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

Note: This only works if EC2 can reach the home network (Cloudflare Tunnel or VPN needed).

---

## Reference: Go2 Internal Network Map

```
Home WiFi (192.168.1.x)
    |
    v
Go2 Internal Router ─── gets 192.168.1.17 on WiFi
    |
    ├── 192.168.123.18  ── Jetson Orin NX (primary compute)
    │                       SSH: unitree/123
    │                       DDS sport service runs here
    │
    ├── 192.168.123.161 ── MCU (motor control board)
    │
    ├── 192.168.123.13? ── Second Jetson (EDU Plus extra compute)
    │
    └── 192.168.123.1   ── Internal router/gateway
        or .100
```

## Reference: Available SDK Commands

| Command | SDK Method | Description |
|---------|-----------|-------------|
| damp | `Damp()` | Disable motors (go limp) |
| stand_up | `StandUp()` | Stand up |
| stand_down | `StandDown()` | Lie down |
| move | `Move(vx, vy, vyaw)` | Velocity move (m/s, m/s, rad/s) |
| stop_move | `StopMove()` | Stop moving |
| hand_stand | `HandStand(bool)` | Handstand (toggle) |
| balance_stand | `BalanceStand()` | Balance on all four legs |
| recovery | `RecoveryStand()` | Get up from fallen |
| left_flip | `LeftFlip()` | Left flip |
| back_flip | `BackFlip()` | Back flip |
| free_walk | `FreeWalk()` | Autonomous walking |
| free_bound | `FreeBound(bool)` | Bounding gait (toggle) |
| free_avoid | `FreeAvoid(bool)` | Obstacle avoidance walk (toggle) |
| walk_upright | `WalkUpright(bool)` | Walk on hind legs (toggle) |
| cross_step | `CrossStep(bool)` | Cross-step dance (toggle) |
| free_jump | `FreeJump(bool)` | Jump (toggle) |
