#!/usr/bin/env bash
# 刷新本机桌面启动器，让菜单图标指向指定（或最新）版本的二进制。
#
# 为什么需要这个：~/.local/share/applications/model-reservoir.desktop 是**手工建的**，
# 程序自己不维护它（app.py 只管 ~/.config/autostart/model-gateway.desktop）。
# 于是每次发版后它就指向旧版本 —— 2026-10-22 用户开机从菜单拉起来拿到的还是
# v1.9.0，弹出「发现新版本 1.9.0 → 2.1.2」，就是这个文件没跟着更新。
#
# 用法：
#   scripts/update-launcher.sh          # 指向 dist/ 里版本号最大的二进制
#   scripts/update-launcher.sh 2.1.2    # 指定版本
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP="${HOME}/.local/share/applications/model-reservoir.desktop"

[ -f "$DESKTOP" ] || { echo "没有启动器文件：$DESKTOP（跳过）"; exit 0; }

if [ $# -ge 1 ]; then
  VER="$1"
else
  # 取 dist/ 下 v*-模型蓄水池 的最新版本（数值逐段比较，不是字符串比较）
  VER="$(ls -1 "$REPO"/dist/v*-模型蓄水池 2>/dev/null \
    | sed -E 's#.*/v([0-9.]+)-模型蓄水池#\1#' \
    | sort -t. -k1,1n -k2,2n -k3,3n \
    | tail -1)"
fi
[ -n "$VER" ] || { echo "dist/ 下没有可指的二进制（跳过）"; exit 0; }

BIN="$REPO/dist/v${VER}-模型蓄水池"
[ -x "$BIN" ] || { echo "二进制不存在或不可执行：$BIN" >&2; exit 1; }

# Exec 与 StartupWMClass 里的版本号一起改，其余内容一律不动
sed -i -E \
  -e "s#dist/v[0-9.]+-模型蓄水池#dist/v${VER}-模型蓄水池#g" \
  -e "s#^StartupWMClass=.*#StartupWMClass=v${VER}-模型蓄水池#" \
  "$DESKTOP"

echo "启动器 → v${VER}"
grep -E '^(Exec|StartupWMClass)=' "$DESKTOP" | sed 's/^/  /'
command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$(dirname "$DESKTOP")" 2>/dev/null \
  || true
