#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按 README.md 安装项目依赖。'
  read -r '?按回车退出。'
  exit 1
fi
exec .venv/bin/python -m maildesk "$@"
