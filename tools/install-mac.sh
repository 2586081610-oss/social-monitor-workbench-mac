#!/bin/sh
# ============================================================
#  install-mac.sh - Mac 独立环境一键安装（幂等，可重复运行）
#  核心阶段（默认执行）:
#    1) uv（用户级 ~/.local/bin，唯一新增全局工具）
#    2) .venv（CPython 3.13.18，uv 托管）+ 四个分层 lock 联合安装 + uv pip check
#    3) Node v22.23.4 LTS arm64（~/Library/Application Support/socialmonitor-node/）
#    4) ffmpeg arm64（imageio-ffmpeg 自带二进制 → tools/bin/ffmpeg，不入库）
#    5) 前端 npm ci + build（dist 缺失时）
#  可选阶段:
#    --upstreams  克隆四上游（jiji262@ed86361 / MediaCrawler@68d9ef1c /
#                 XHS-Downloader@47840a1b=tag2.8 / RSSHub@v2026.10.26-8483.9515261，
#                 均不入库）并把 jiji262、XHS-Downloader 的依赖装进主 venv
#                 （download.py 以主 venv 解释器直调）；MediaCrawler 用 uv sync
#                 建自己的 .venv；RSSHub 依赖需 corepack pnpm（打印命令，不自动跑）
#    --browsers   playwright install chromium（cookie 导出 chromium 通道需要）
#  本脚本不设置代理、不动系统 Python；网络走用户现有环境。
# ============================================================
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NODE_VERSION="v22.23.4"
NODE_DIR="${NODE_DIR:-$HOME/Library/Application Support/socialmonitor-node}"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"

say() { echo "[install-mac] $*"; }

# ---- 1. uv ----
if [ ! -x "$UV_BIN" ]; then
  say "安装 uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
"$UV_BIN" --version

# ---- 2. venv + 依赖 ----
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  say "创建 .venv（CPython 3.13.18）..."
  "$UV_BIN" venv "$ROOT/.venv" --python 3.13.18
fi
say "安装四组分层 lock 并做一致性检查 ..."
"$UV_BIN" pip install -r "$ROOT/requirements.lock" -r "$ROOT/requirements-download.lock" \
  -r "$ROOT/requirements-asr.lock" -r "$ROOT/requirements-browser.lock" --python "$ROOT/.venv/bin/python"
"$UV_BIN" pip check --python "$ROOT/.venv/bin/python"

# ---- 3. Node（用户级目录，不污染系统） ----
if [ ! -x "$NODE_DIR/current/bin/node" ]; then
  say "下载 Node ${NODE_VERSION} darwin-arm64 ..."
  mkdir -p "$NODE_DIR"
  curl -sSL -o "$NODE_DIR/node.tar.xz" "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-darwin-arm64.tar.xz"
  tar -xzf "$NODE_DIR/node.tar.xz" -C "$NODE_DIR"
  ln -sfn "$NODE_DIR/node-${NODE_VERSION}-darwin-arm64" "$NODE_DIR/current"
  rm -f "$NODE_DIR/node.tar.xz"
fi
"$NODE_DIR/current/bin/node" --version

# ---- 4. ffmpeg（与 Windows 同来源族：imageio-ffmpeg 自带二进制） ----
if [ ! -x "$ROOT/tools/bin/ffmpeg" ]; then
  say "落位 ffmpeg 到 tools/bin/ffmpeg ..."
  FFMPEG_SRC="$("$ROOT/.venv/bin/python" -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")"
  mkdir -p "$ROOT/tools/bin"
  cp "$FFMPEG_SRC" "$ROOT/tools/bin/ffmpeg"
  chmod +x "$ROOT/tools/bin/ffmpeg"
fi
"$ROOT/tools/bin/ffmpeg" -version 2>/dev/null | head -1 || { say "[ERROR] ffmpeg 不可用"; exit 1; }

# ---- 5. 前端构建 ----
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  say "构建前端（npm ci + build）..."
  (cd "$ROOT/frontend" && PATH="$NODE_DIR/current/bin:$PATH" npm ci --no-audit --no-fund && PATH="$NODE_DIR/current/bin:$PATH" npm run build)
fi

# ---- 可选: 上游锁定版本获取 ----
if [ "${1:-}" = "--upstreams" ]; then
  # 优先 git clone（保留 .git 便于核对 HEAD）；github.com 直连不可用时回退
  # codeload tarball（按 commit/tag 取包，写 UPSTREAM_SOURCE.json 记录来源与 sha256）
  fetch() { # $1=dir $2=repo $3=ref
    if [ -d "$ROOT/tools/$1" ]; then
      say "上游 $1 已存在，跳过（来源见 tools/$1/UPSTREAM_SOURCE.json 或 .git）"
      return 0
    fi
    if git ls-remote "https://github.com/$2" >/dev/null 2>&1; then
      say "克隆 $1 @ $3 ..."
      git clone --quiet "https://github.com/$2" "$ROOT/tools/$1"
      git -C "$ROOT/tools/$1" checkout --quiet --detach "$3"
    else
      say "github.com 不可达，改用 codeload tarball 获取 $1 @ $3 ..."
      tmp="$(mktemp "${TMPDIR:-/tmp}/upper.XXXXXX.tar.gz")"
      curl -sSL --max-time 600 -o "$tmp" "https://codeload.github.com/$2/tar.gz/$3"
      mkdir -p "$ROOT/tools/$1"
      tar -xzf "$tmp" -C "$ROOT/tools/$1" --strip-components 1
      tar_sha="$(shasum -a 256 "$tmp" | cut -d' ' -f1)"
      rm -f "$tmp"
      "$ROOT/.venv/bin/python" - "$1" "$2" "$3" "$tar_sha" <<'PY'
import json, sys
from datetime import datetime, timezone
d, repo, ref, sha = sys.argv[1:5]
json.dump({"repo": repo, "ref": ref, "tarball_sha256": sha,
           "fetched_at": datetime.now(timezone.utc).isoformat(),
           "method": "codeload-tarball"},
          open(f"tools/{d}/UPSTREAM_SOURCE.json", "w"), indent=1)
PY
    fi
  }
  fetch douyin-downloader jiji262/douyin-downloader ed863617aa52353821aca8fdebcfad4e916ebff5
  fetch mediacrawler NanmiCoder/MediaCrawler 68d9ef1c1da22eab75940dfd7cec259907006538
  fetch xhs-downloader JoeanAmier/XHS-Downloader 47840a1bee8438324ff10753c4291148c46071c8
  fetch rsshub DIYgod/RSSHub refs/tags/v2026.10.26-8483.9515261

  say "把 jiji262 / XHS-Downloader 依赖装入主 venv（download.py 以主解释器直调）..."
  for req in douyin-downloader/requirements.txt xhs-downloader/requirements.txt; do
    if [ -f "$ROOT/tools/$req" ]; then
      "$UV_BIN" pip install -r "$ROOT/tools/$req" --python "$ROOT/.venv/bin/python" || say "[WARN] $req 安装失败，请人工核对"
    else
      say "[WARN] $req 不存在，跳过（核对上游目录结构）"
    fi
  done
  "$UV_BIN" pip check --python "$ROOT/.venv/bin/python"

  say "创建 MediaCrawler 独立 .venv（uv sync）..."
  (cd "$ROOT/tools/mediacrawler" && "$UV_BIN" sync) || say "[WARN] MediaCrawler uv sync 失败，请人工核对"

  say "RSSHub 依赖未自动安装。手动执行："
  echo "  cd '$ROOT/tools/rsshub' && corepack enable && pnpm install"
fi

# ---- 可选: playwright chromium ----
if [ "${1:-}" = "--browsers" ]; then
  say "安装 playwright chromium ..."
  "$ROOT/.venv/bin/playwright" install chromium
fi

say "完成。启动: ./start_all.sh（默认后端+前端）；隔离验收: ./start_all.sh --sandbox"
say "cookie 导出（需真实登录，用户操作）: python tools/export_dy_cookie_mac.py / export_xhs_cookie_mac.py"
