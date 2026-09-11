#!/usr/bin/env bash
# Debian: bash scripts/deploy-cpa.sh [Docker Compose global options]
set -Eeuo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  printf '用法: %s [Compose 参数]\n例: %s -f compose.yaml -f compose.cpa.yaml\n' "$0" "$0"
  exit 0
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

stage="检查环境"
stopped=false
on_error() {
  local status=$?
  printf '\n发版失败：%s（退出码 %s）。\n' "$stage" "$status" >&2
  if [[ "$stopped" == true ]]; then
    printf '服务已停止，请修复报错后重新运行脚本。\n' >&2
  fi
  exit "$status"
}
trap on_error ERR

command -v git >/dev/null
command -v docker >/dev/null
docker compose version >/dev/null
docker info >/dev/null
if [[ "$(git branch --show-current)" != "cpa" ]]; then
  printf '请在 cpa 分支运行此脚本。\n' >&2
  exit 1
fi

stage="拉取 cpa 最新代码"
printf '\n[1/4] %s\n' "$stage"
git pull --ff-only

# Validate the effective configuration before stopping the existing service.
stage="检查 Compose 镜像配置"
images=$(docker compose "$@" config --images)
if ! grep -Fxq 'sub2pool-cpa:local' <<< "$images"; then
  printf 'Compose 未使用 sub2pool-cpa:local；请将 app.image 配为该镜像，或传入对应的 -f 覆盖文件。\n' >&2
  exit 1
fi

stage="停止容器"
printf '\n[2/4] %s\n' "$stage"
docker compose "$@" down
stopped=true

stage="构建 sub2pool-cpa:local"
printf '\n[3/4] %s\n' "$stage"
docker build -t sub2pool-cpa:local .

stage="启动容器"
printf '\n[4/4] %s\n' "$stage"
docker compose "$@" up -d
stopped=false
docker compose "$@" ps
printf '\n发版启动完成。迁移及历史重放由容器启动脚本执行，可用 docker compose logs -f app 查看进度（沿用本次 Compose 参数）。\n'
