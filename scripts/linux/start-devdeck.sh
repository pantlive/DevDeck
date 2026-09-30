#!/usr/bin/env bash
# 安装并启动持久的 DevDeck 用户服务；可选保持当前 WSL 会话，避免用户 systemd 退出。
set -euo pipefail

python=""
project_dir=""
port="7310"
data_dir=""
unit_name="devdeck.service"
hold="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python="$2"
      shift 2
      ;;
    --project-dir)
      project_dir="$2"
      shift 2
      ;;
    --port)
      port="$2"
      shift 2
      ;;
    --data-dir)
      data_dir="$2"
      shift 2
      ;;
    --unit)
      unit_name="$2"
      shift 2
      ;;
    --hold)
      hold="1"
      shift
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$python" || -z "$project_dir" || -z "$data_dir" ]]; then
  echo "必须提供 --python、--project-dir 和 --data-dir" >&2
  exit 2
fi

mkdir -p "$data_dir"
unit_path="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${unit_name}"
log_path="${data_dir}/devdeck-startup.log"

cd "$project_dir"
"$python" -m devdeck.autostart \
  --unit-path "$unit_path" \
  --python "$python" \
  --project-dir "$project_dir" \
  --port "$port" \
  --data-dir "$data_dir" \
  --log-path "$log_path"

# 用户总线在 WSL 刚起来时可能尚未就绪，短重试。
for _ in $(seq 1 30); do
  if systemctl --user daemon-reload && systemctl --user enable "$unit_name"; then
    break
  fi
  sleep 1
done

if systemctl --user is-active --quiet "$unit_name"; then
  :
else
  systemctl --user start "$unit_name"
fi

port_ready="0"
for _ in $(seq 1 30); do
  if systemctl --user is-active --quiet "$unit_name" \
    && "$python" -c "import socket; s=socket.create_connection(('127.0.0.1', int('$port')), 0.2); s.close()"; then
    port_ready="1"
    break
  fi
  sleep 1
done

if [[ "$port_ready" != "1" ]]; then
  systemctl --user status "$unit_name" --no-pager -l >&2 || true
  exit 1
fi

if [[ "$hold" == "1" ]]; then
  # 保持本 WSL 客户端会话，阻止用户 systemd 在 wsl.exe 退出后进入 exit.target。
  while systemctl --user is-active --quiet "$unit_name"; do
    sleep 30
  done
fi
