#!/bin/sh
# ============================================================
#  start_rsshub.sh - 启动自建 RSSHub（推特/B站博主动态路由）
#  源码位置：workbench 同级目录 ../rsshub（tsx 直跑源码模式，
#  构建产物 dist 结构与运行时不匹配，勿用 node dist/index.mjs）。
#  推特路由需要 ../rsshub/.env 配置 TWITTER_AUTH_TOKEN。
#  停止：pkill -f "tsx lib/index.ts"
# ============================================================
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
RSSHUB_DIR="${RSSHUB_DIR:-$(cd "$ROOT/.." && pwd)/rsshub}"
[ -d "$RSSHUB_DIR" ] || { echo "[ERROR] RSSHub 源码缺失: $RSSHUB_DIR"; exit 1; }
[ -f "$RSSHUB_DIR/.env" ] || { echo "[ERROR] 缺少 $RSSHUB_DIR/.env（至少含 TWITTER_AUTH_TOKEN）"; exit 1; }

if curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:1200/ >/dev/null 2>&1; then
  echo "[提示] RSSHub 已在 1200 端口运行，跳过启动"
  exit 0
fi

cd "$RSSHUB_DIR"
NODE_ENV=dev NODE_OPTIONS='--max-http-header-size=32768' \
  nohup ./node_modules/.bin/tsx lib/index.ts > "$ROOT/.run/logs/rsshub.log" 2>&1 &
echo $! > "$ROOT/.run/rsshub.pid"
sleep 6
if curl --noproxy '*' -fsS --max-time 3 http://127.0.0.1:1200/ >/dev/null 2>&1; then
  echo "[OK] RSSHub 已启动（http://127.0.0.1:1200，日志 .run/logs/rsshub.log）"
else
  echo "[ERROR] RSSHub 启动失败，查看 $ROOT/.run/logs/rsshub.log"
  exit 1
fi
