#!/bin/bash
# 组队协调服务 · 阿里云部署脚本
#
# ⚠️ 运行前请先确认：本脚本会写入阿里云生产机 /opt/reservoir-team/ 与 /etc/systemd/system/
#    并新建一个 systemd 服务。不会改动 frps / 1Panel 既有站点 / 任何现有服务。
#
# 用法（在本机跑，会通过 SSH 推过去）：
#   SSHPASS='<阿里云root口令>' bash team/deploy.sh
#
# 幂等：重复运行只会覆盖 coordinator.py 与单元文件并重启服务，数据文件不动。

set -euo pipefail

HOST=123.57.133.217
PORT=2121
REMOTE_DIR=/opt/reservoir-team
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# IdentityAgent=none 是必须的：本机 ssh-agent 无响应会拖死会话（30s 超时）
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o IdentityAgent=none -o IPQoS=none)
SSH=(sshpass -e ssh "${SSH_OPTS[@]}" -p "$PORT" "root@$HOST")
SCP=(sshpass -e scp "${SSH_OPTS[@]}" -P "$PORT")

step() { echo; echo ">>> $*"; }
die()  { echo "!! 失败: $*" >&2; exit 1; }

[ -n "${SSHPASS:-}" ] || die "请设置 SSHPASS 环境变量（阿里云 root 口令）"

step "0) 连通性与前置检查"
"${SSH[@]}" 'set -e
  echo "  主机: $(hostname)"
  echo "  python3: $(python3 --version 2>&1)"
  python3 -c "import cryptography; print(\"  cryptography:\", cryptography.__version__)" \
    || die "缺 cryptography，先装：pip3 install --break-system-packages cryptography"
  ss -ltn | grep -q ":5022 " && echo "  ⚠ 5022 已被占用" || echo "  5022 空闲 ✓"
  echo "  内存: $(free -h | awk "/Mem/{print \$7}") 可用"
' || die "前置检查失败"

step "1) 上传协调服务与单元文件"
"${SSH[@]}" "mkdir -p $REMOTE_DIR"
"${SCP[@]}" "$LOCAL_DIR/coordinator.py" "root@$HOST:$REMOTE_DIR/coordinator.py"
"${SCP[@]}" "$LOCAL_DIR/../protocol.py" "root@$HOST:$REMOTE_DIR/protocol.py"
echo "  已上传 coordinator.py + protocol.py（密钥派生协议，两边共用避免漂移）"
"${SCP[@]}" "$LOCAL_DIR/reservoir-team.service" "root@$HOST:/etc/systemd/system/reservoir-team.service"
echo "  已上传"

step "1b) 校验 protocol.py 可被 coordinator 导入"
"${SSH[@]}" "cd $REMOTE_DIR && python3 -c 'import protocol; print(\"  protocol 导入 OK, N=\", protocol.SCRYPT_N)'" || die "protocol 导入失败"

step "2) 保留既有数据（team.db 不覆盖）"
"${SSH[@]}" "if [ -f $REMOTE_DIR/team.db ]; then
    cp -a $REMOTE_DIR/team.db $REMOTE_DIR/team.db.bak.$(date +%Y%m%d_%H%M%S)
    echo '  已备份现有 team.db'
  else
    echo '  首次部署，无既有数据'
  fi"

step "3) 启用并重启 systemd 服务（必须 restart，enable --now 不会重启已在跑的进程）"
"${SSH[@]}" 'set -e
  systemctl daemon-reload
  systemctl enable reservoir-team
  # 关键：文件已更新但服务可能还在跑旧代码，enable --now 对已运行进程无效
  systemctl restart reservoir-team
  sleep 3
  systemctl is-active reservoir-team && echo "  服务 active ✓" || { echo "  服务未启动"; journalctl -u reservoir-team -n 20 --no-pager; exit 1; }
  echo "  启动时间: $(systemctl show reservoir-team -p ExecMainStartTimestamp --value)"
'

step "4) 本机回环健康检查（不经 nginx）"
"${SSH[@]}" 'curl -s -m 5 http://127.0.0.1:5022/health && echo || { echo "  健康检查失败"; journalctl -u reservoir-team -n 20 --no-pager; exit 1; }'

step "5) 资源占用确认"
"${SSH[@]}" 'systemctl show reservoir-team -p MemoryPeak -p MemoryCurrent 2>/dev/null | sed "s/^/  /"
  echo "  当前内存: $(free -h | awk "/Mem/{print \$7}") 可用"'

step "5b) 确认线上代码是新版（join 必须返回 code_salt）"
"${SSH[@]}" 'if grep -q "code_salt.*t\[.code_salt.\]" /opt/reservoir-team/coordinator.py; then
    echo "  ✓ coordinator.py 含 salt 返回逻辑"
  else
    echo "  ✗ coordinator.py 仍是旧版！"; exit 1
  fi
  if [ -f /opt/reservoir-team/protocol.py ]; then echo "  ✓ protocol.py 已就位"; else echo "  ✗ protocol.py 缺失"; exit 1; fi
'

step "6) systemd 自启与重启策略确认"
"${SSH[@]}" 'systemctl is-enabled reservoir-team | sed "s/^/  enabled: /"
  systemctl show reservoir-team -p Restart -p MemoryMax | sed "s/^/  /"'

echo
echo "=========================================="
echo "  协调服务已部署并运行"
echo "  回环健康: http://127.0.0.1:5022/health"
echo "  公网入口: https://model-reservoir.heilongxiake.cn  ← 需在 1Panel 建站点反代到 5022"
echo "  日志:     journalctl -u reservoir-team -f"
echo "=========================================="
echo
echo "后续（需在 1Panel 面板操作，本脚本不代劳）："
echo "  1. 1Panel → 网站 → 创建反向代理站点"
echo "     域名 model-reservoir.heilongxiake.cn，上游 http://127.0.0.1:5022"
echo "     证书选通配符 *.heilongxiake.cn"
echo "  2. 参考 team/nginx-model-reservoir.conf 里的 location 配置"
echo "  3. 实测: curl -s https://model-reservoir.heilongxiake.cn/health"
