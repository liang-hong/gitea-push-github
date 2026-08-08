# 同步工具镜像
# 构建: docker build -t ghcr.io/<your-account>/gitea-github-sync:latest sync/
# 使用: 参考仓库根目录 .woodpecker.yml 模板
FROM python:3.11-alpine

# 安装同步脚本到 PATH
COPY gitea_github_sync.py /usr/local/bin/gitea_github_sync.py
RUN chmod +x /usr/local/bin/gitea_github_sync.py

# 默认工作目录（Woodpecker 挂载源码的位置）
WORKDIR /workspace
