#!/usr/bin/env bash
# 清空某个 media 的解析状态，使下一次分析走「冷路径」（重新 ASR/OCR、重新规划）。
# 仅用于基准测试，不改产品代码。密码从仓库根目录的 .env 读取，不硬编码。
set -uo pipefail
MEDIA_ID="${1:?usage: reset-media.sh <mediaId>}"

ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT" || exit 1

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required (set it in .env)}"

docker compose --env-file .env exec -T mysql sh -c \
  "mysql -uroot -p${MYSQL_ROOT_PASSWORD} media_db -e \
   \"delete from agent_checkpoints where media_id=${MEDIA_ID}; delete from failed_analysis_tasks where media_id=${MEDIA_ID};\"" \
  >/dev/null 2>&1

docker compose --env-file .env exec -T redis sh -c '
  for p in "agent:checkpoint:'"${MEDIA_ID}"'*" "agent:trace:task:'"${MEDIA_ID}"'*" "analysis:completed:*" "analysis:context-owner:*"; do
    redis-cli -a "$REDIS_PASSWORD" --no-auth-warning --scan --pattern "$p" \
      | xargs -r redis-cli -a "$REDIS_PASSWORD" --no-auth-warning del >/dev/null
  done' >/dev/null 2>&1

echo "reset media ${MEDIA_ID}"
