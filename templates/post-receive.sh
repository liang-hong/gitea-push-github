#!/usr/bin/env bash
# Gitea 仓库 post-receive hook 模板（方案 A：push 后即时初始化 GitHub 镜像）
#
# 配置方法：Gitea 仓库 -> 设置 -> Git Hooks -> post-receive，粘贴本脚本。
# 这是 Gitea 官方文档认可的 SSH/按需推送方式，push 到 Gitea 后立即调用
# 本仓库的 sync 脚本完成“GitHub 建库 + 配置 Push Mirror”（幂等）。
#
# 占位符：
#   {{SYNC_SCRIPT}}  本仓库 sync/gitea_github_sync.py 的绝对路径
#   {{CREDENTIALS}}  本地凭据文件（形如 --credentials /path/xxx.env；
#                    留空则使用脚本默认 ~/.config/gitea-push-github/...）
#
# 日志目录先建一次：mkdir -p ~/.local/log（无 /var/log 写权限时可用此用户目录）
#
# Gitea 为 git hook 提供 GITEA_REPO_USER_NAME / GITEA_REPO_NAME 环境变量。
set -u

exec {{SYNC_SCRIPT}} \
  --repo-owner "$GITEA_REPO_USER_NAME" \
  --repo-name "$GITEA_REPO_NAME" \
  {{CREDENTIALS}} \
  >> ~/.local/log/gitea-push-github.log 2>&1
