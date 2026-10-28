# tools/ 工具链说明

博主监控工作台下载引擎与辅助工具就绪清单。所有工具位于本目录，供监控层流水线
（`monitor/pipeline/download.py`）以**子进程方式**调用（许可证进程隔离）。

| 工具 | 平台 | 用途 | 位置 | 状态 |
|------|------|------|------|------|
| yutto 2.3.0 | B站 | 视频下载（多清晰度，流水线用 360p） | Python Scripts + `bin/yutto.cmd` 包装 | 就绪 |
| ffmpeg 7.1 | 全平台 | 抽帧/转码 | `bin/ffmpeg.exe` | 就绪 |
| jiji262/douyin-downloader | 抖音 | 视频/图集下载（最低画质） | `douyin-downloader/` | 就绪 |
| XHS-Downloader | 小红书 | 图文/视频下载（GPL-3.0） | `xhs-downloader/` | 就绪（源码） |
| MediaCrawler | 全平台 | 采集兜底（预留，非商业许可） | 未落地 | 预留 |

---

## 1. yutto（B站下载器）

- **安装方式**：managed Python 3.13.12 安装
  ```bash
  C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pip install --no-cache-dir yutto
  ```
- **安装位置**：`C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\Scripts\yutto.exe`
  （pip 默认 Scripts 目录不在 PATH，故提供包装脚本 `tools/bin/yutto.cmd`）
- **验证**：`python -m yutto --version` → `yutto 2.3.0`
- **流水线调用**：`yutto <url> -d <media_dir> -q 360p`（`shutil.which("yutto")` 经
  `tools/bin` 加入 PATH 后解析到 `yutto.cmd`）
- **注意事项**：
  - B站下载可能触发风控，需在 yutto 交互登录或用 `-c` 指定 cookie 文件；
    正式使用请参考官方文档（yutto-dev/yutto，以 GitHub 为准）。
  - yutto 默认输出目录带多级结构，`_find_video` 只扫描一级目录，若未命中请手动指定 `-d` 的扁平目录。

---

## 2. ffmpeg（系统级，抽帧用）

- **来源**：`imageio_ffmpeg` 包自带二进制（ffmpeg 7.1 essentials build）
  ```
  C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe
  ```
- **落地**：已复制为 `tools/bin/ffmpeg.exe`，并附 `ffmpeg.cmd` 兜底包装
- **验证**：`tools/bin/ffmpeg.exe -version`
- **PATH 说明（两种方式任选）**：
  1. 一键脚本：`start_all.bat` 已自动将 `tools\bin` 加入进程 PATH（推荐，仅对启动的进程生效）
  2. 全局 PATH：控制面板 → 系统属性 → 环境变量 → Path 增加 `C:\Users\Administrator\WorkBuddy\2026-08-22-18-25-30\tools\bin`，
     重开终端后 `ffmpeg -version` 可直接执行
- **流水线调用**：`monitor/pipeline/frames.py` 直接 `subprocess.run(["ffmpeg", ...])`，
  依赖 ffmpeg 在 PATH 中（`tools\bin` 已满足）。

---

## 3. douyin-downloader（jiji262，抖音）

- **位置**：`tools/douyin-downloader/`（已就绪）
- **启动方式**（供流水线调用，cwd 必须在仓库根目录）：
  ```bash
  cd tools/douyin-downloader
  C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe run.py -c config.yml -u <url> -p <输出目录>
  ```
- **配置**：最小配置（流水线自动生成 `config.yml`）：
  ```yaml
  video_quality: lowest
  thread: 3
  retry_times: 3
  progress:
    quiet_logs: true
  ```
- **环境变量**：`DOUYIN_DOWNLOADER_DIR` 指向本目录（download.py 据此定位）
- **注意事项**：
  - 抖音风控严格，建议使用 Playwright 持久化 profile（见 §5）人工过验证码后复用登录态；
  - 依赖安装：`python -m pip install -r requirements.txt`（如未装）。

---

## 4. XHS-Downloader（小红书，GPL-3.0）

- **仓库**：JoeanAmier/XHS-Downloader（GPL-3.0，进程隔离调用不内嵌代码），默认分支 `master`
- **落地状态**：`tools/xhs-downloader/`（源码已就绪，依赖已按 `requirements.txt` 安装）
- **启动方式**（版本以官方 GitHub README 为准，默认分支 master）：
  ```bash
  cd tools/xhs-downloader
  # TUI 交互模式（默认）
  C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe main.py
  # API 模式（端口 5556，适合二次开发/流水线）
  C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe main.py api
  # 命令行模式（直接下载）
  C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m source.CLI.main -u "小红书链接"
  ```
- **注意事项**：
  - 小红书强风控，**Cookie 非强制**但推荐配置：不设置 Cookie 时视频只能下载低分辨率；
    手动获取 Cookie 写入 `Volume/settings.json`（配置方法见官方 README）；
  - 本项目流水线仅以 `xhs <url> -d <media_dir>` 契约调用（download.py 预留），
    接入前需按官方 README 校准参数与登录方式；
  - 未完整跑通前不阻塞主流程：监控层对 xhs 平台先走 RSSHub 采集（platforms/rsshub.py）；
  - 需要 Python >= 3.12（当前 3.13.12 满足）。

---

## 5. Cookie 方案（抖音/小红书共用）

- **目录**：`tools/dy_profile/` — Playwright 持久化浏览器 profile
  （Chrome 用户数据目录，含登录态 cookie）
- **用途**：抖音 CDP 轮询（`ENABLE_DOUYIN_CDP=true` 时）与下载器复用登录态，
  减少人工过验证码次数
- **配套脚本**：`tools/extract_douyin_cookie.py` — 提取抖音 cookie 到
  `config/cookies.json`（供需要 cookie 的下载器使用）
- **刷新时机**：登录态过期或风控后重新登录，建议每 1~2 周人工刷新一次。

---

## 6. 一键启停

| 脚本 | 作用 |
|------|------|
| `start_all.bat`（工作区根） | 依次启动 后端(:8000) + 前端(:5173) + 监控层(monitor/main.py)，自动把 `tools\bin` 与 Python Scripts 加入 PATH |
| `stop_all.bat`（工作区根） | 按 窗口标题 → 监听端口 → 命令行 三层兜底停止全部服务 |

- 前端若 `dist` 未构建，先执行：`cd frontend && npm run build`
- 重复启动前务必先 `stop_all.bat`，避免 8000/5173 端口冲突。

---

## 7. Mac（arm64）安装与版本锚定

- **一键安装**：`tools/install-mac.sh`（核心：uv + `.venv`(CPython 3.13.18) + 四个分层 lock + Node + ffmpeg + 前端构建；`--upstreams` 克隆四上游并装各自依赖；`--browsers` 装 Playwright chromium）。幂等可重复运行；不装 brew/docker，不改系统 Python。
- **启停**：`start_all.sh`（默认只启动后端+前端；`--sandbox` 隔离验收）、`start_monitor.sh`（显式启动监控层——其启动即轮询）、`stop_all.sh`（只停止 PID 记录且身份核验通过的本项目进程；端口被无归属进程占用时仅报告）。
- **版本锚定**（与 `preflight/mac-adaptation-plan.md` §1 一致，克隆均不入库）：

| 上游 | 锚定 | 说明 |
|------|------|------|
| jiji262/douyin-downloader | commit `ed863617aa52353821aca8fdebcfad4e916ebff5` | **官方 README（2026-10-28）声明：CLI 单视频/图文/合集/音乐/喜欢/收藏下载受抖音风控，更新 Cookie 或重试无解**；参数契约吻合≠可用，C2 实测前不宣称可用 |
| MediaCrawler | commit `68d9ef1c1da22eab75940dfd7cec259907006538` | 无 release/tag；独立 `.venv`（uv sync） |
| XHS-Downloader | tag `2.8` = commit `47840a1bee8438324ff10753c4291148c46071c8` | 依赖装入主 venv（download.py 以主解释器直调） |
| RSSHub | tag `v2026.10.26-8483.9515261` | Node 22 + corepack pnpm 12.15.0；启动 `NODE_ENV=production node dist/index.mjs`；B站路由依赖 Playwright chromium（`pnpm dlx playwright install chromium`，用户级缓存）。已实测：公开用户路由拉取 40 条正常（该路由无发布时间字段，归档按未知时间处理） |

- **分层 lock**（仓库根，`uv pip compile` 产物）：`requirements.lock`（核心9项）、`requirements-download.lock`（yutto 2.3.1 / yt-dlp 2026.10.26 / curl-cffi 0.17.0）、`requirements-asr.lock`（funasr 1.4.16 / torch 2.14.1 / imageio-ffmpeg 0.6.0）、`requirements-browser.lock`（playwright 1.64.1）。
- **ffmpeg**：`imageio-ffmpeg`（0.6.0 为分发器版本）自带 arm64 二进制 → `tools/bin/ffmpeg`（不入库，`tools/bin/*.exe` 保留给 Windows）。
- **Mac cookie 导出**：`tools/export_dy_cookie_mac.py`、`tools/export_xhs_cookie_mac.py`（输出格式与 Windows 版一致；浏览器通道 edge → chrome → chromium 自动降级；`--dry-run` 只验证环境）。
- **ASR 模型**（C2/D 阶段下载）：paraformer-large / fsmn-vad / ct-punc-c 三个 ModelScope 仓库均为 rolling `master`，下载后生成 sha256 manifest 并在 `transcribe.py` 显式 `revision`。
- **WorkBuddy**：复用本机已装应用（/Applications/WorkBuddy.app），项目不安装、不依赖其托管运行时。

### Mac 启停与本人来源导入

`./start_all.sh --sandbox` 将持久化目录指向项目 `.sandbox/`；不加该参数则使用现有配置。默认只启动 API 和前端，**后端归档 worker 仍会恢复并执行待处理归档任务，可能更新信息文件与清单**；试验时请使用隔离数据。平台轮询和下载流水线不会随此脚本启动。

启动脚本核对服务进程与 HTTP 探针，通过后才报告成功；失败时仅清理本次登记且身份匹配的进程。前端端口冲突直接失败，不换端口。`.run/*.identity` 记录启动时间与完整命令，停止时与 PID 一同核对。旧版本只有 PID 而无身份文件的存活进程、身份不匹配的进程均保留并提示人工核对；端口占用仅报告。启停互斥锁位于 `.run/control.lock`，若操作被强制中断，需先确认没有启停操作仍在进行，再移除遗留空锁目录。`NO_PROXY` 与 `no_proxy` 分别保留并补齐 `localhost,127.0.0.1,::1`，不更改上游代理配置。

本人来源默认关闭；显式导入示例：

```sh
.venv/bin/python -m monitor.personal_source_ingest --source file --base-dir /path/to/ddr-data --dry-run
.venv/bin/python -m monitor.personal_source_ingest --source file --base-dir /path/to/ddr-data
```

`--dry-run` 在内存副本上预演，不创建、迁移或修改配置指向的数据库与存储目录；已有数据库存在未检查点的 WAL 时显式拒绝预演，请先停止服务并完成检查点。缺失文件、无效 JSON、未知 schema 与损坏文件夹关系会明确失败，不作为空列表处理。无效日期按未知处理，不用导入时间代替。

该通道只登记本人点赞、平铺收藏与 DDR 应用内本地单文件夹事实；不回写来源、不自动下载、不代表已支持抖音平台完整收藏夹或一作多夹同步。下载仍需在原工作台显式触发，沿用原下载器和归档服务。MediaCrawler 的超时、异常输出及上游错误会记录失败；真实空列表才表示成功且无新作品。

**Mac 抖音单视频备用路径**：仍先调用原 jiji262 下载器；当它报错或没有产出原媒体时，工作台会尝试使用独立的临时 Chromium 会话打开公开作品，并仅从该作品的详情数据所对应的视频与音频流中取回完整文件，使用 FFmpeg 合并及校验。备用路径不使用现有浏览器登录态，也不修改原下载器配置；需要项目 Playwright 依赖、Chromium 和 FFmpeg 已安装。私密作品、登录墙、平台响应格式变化或访问受限时可能失败，失败不会伪造下载完成记录。Windows 原路径保持不变。
