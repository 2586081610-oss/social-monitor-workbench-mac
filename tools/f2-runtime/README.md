# F2 隔离运行时

固定 commit 的 [F2](https://github.com/Johnserf-Seed/f2) 只安装在本目录 `.venv`
（不进主环境、不进 backend requirements），供 `monitor/platforms/f2_fetch.py`
以子进程方式读取本人抖音点赞 / 平铺收藏 / 收藏夹 / 夹内作品（只读列表接口）。

## 安装（Mac，已完成；重装时执行）

```bash
uv venv --python 3.13 .venv
HTTPS_PROXY=<本机代理> uv pip install --python .venv/bin/python \
  -r requirements-f2.txt
```

版本身份以 `UPSTREAM_SOURCE.json` 为准（源码版本号仍显示 0.0.1.7，以 commit 为锚）。

## 用户前置条件（登录由用户本人操作）

1. 隔离运行时已安装（本目录 `.venv` 存在）。
2. 登录态 cookie 文本文件：默认路径 `config/f2/douyin_cookie.txt`（config/ 不进 Git），
   或用环境变量 `F2_DOUYIN_COOKIE_FILE` 指向任意本地文件。
   内容为抖音网页版登录后的整串 Cookie（浏览器 DevTools 复制）。文件只在本机读取，
   不打印、不入库、不进 Git。
3. 本人 `sec_user_id`（抖音网页版个人主页 URL 中的 sec_uid），存于工作台
   「个人来源」定时配置中，点赞列表需要。

## 运行时环境变量

- `F2_RUNTIME_DIR`：覆盖运行时目录（默认 `tools/f2-runtime`）。
- `F2_DOUYIN_COOKIE_FILE`：覆盖 cookie 文件路径（默认 `config/f2/douyin_cookie.txt`）。

## 失败语义

登录过期 / 未授权 / 限流 / 超时 / 运行时缺失都会以明确 kind 上报
（`login_expired` / `like_list_unavailable` / `rate_limited` / `timeout` /
`runtime_missing` / `already_running`），绝不把失败伪装成空列表。
