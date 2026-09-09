#!/usr/bin/env bash
# DeepSeek Harness 容器入口（dsh-switch 部署器生成）
#
# 关键约束（dsh 官方硬限制）：
#   dsh web 只能监听 127.0.0.1（CLI 拒绝 0.0.0.0，防 RCE 暴露）。
#   因此容器用 network_mode: host，dsh 直接绑定宿主机回环地址，
#   远程访问唯一入口 = SSH 隧道（本机端口 -> 服务器 127.0.0.1:__PORT__）。
#
# 另一个坑：必须用 `node --expose-internals <dsh>/lib/bin.js` 启动，
#   不能走 dsh 的 shebang（cordis-plugin-hmr 要求 internal 已暴露，
#   shebang 另起的 Node 进程不带该 flag，会直接崩）。
#
# 环境变量（由 docker-compose.yml 注入）：
#   DSH_PORT          dsh 监听的宿主回环端口
#   DSH_TRUSTED_HOST  追加的 --trusted-host（空格分隔，可留空）
#   DSH_EXTRA_ARGS    追加给 dsh 的其他参数
set -uo pipefail

PORT="${DSH_PORT:-3080}"
HOME_DIR="${DSH_HOME:-/data/dsh}"
# dsh CLI 的真实入口（npm 全局安装路径）
DSH_CLI="/usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js"

mkdir -p "$HOME_DIR"

# 组装 --trusted-host（去重；回环 authority 总是补上）
_seen=""
TRUSTED=()
add_trusted() {
  for _h in "$@"; do
    [ -z "$_h" ] && continue
    case " $_seen " in
      *" $_h "*) continue ;;
    esac
    _seen="$_seen $_h"
    TRUSTED+=("--trusted-host" "$_h")
  done
}

[ -n "${DSH_TRUSTED_HOST:-}" ] && read -r -a _extra <<< "$DSH_TRUSTED_HOST" && add_trusted "${_extra[@]}"
add_trusted "localhost:${PORT}" "127.0.0.1:${PORT}"

stop() {
  [ -n "${DSH_PID:-}" ] && kill "$DSH_PID" 2>/dev/null
  exit 0
}
trap stop INT TERM HUP

echo "[entrypoint] dsh -> 127.0.0.1:${PORT} (--expose-internals, host network)"
echo "[entrypoint] trusted-host: ${TRUSTED[*]}"

node --expose-internals "$DSH_CLI" web --no-open --host 127.0.0.1 \
     --port "$PORT" "${TRUSTED[@]}" ${DSH_EXTRA_ARGS:-} &
DSH_PID=$!

wait "$DSH_PID"
exit $?
