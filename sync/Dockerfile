# 同步工具镜像
# 构建: docker build -t ghcr.io/<your-account>/gitea-github-sync:latest sync/
# 使用: 由 provision 工具注入的 .woodpecker.yml 模板引用（见 templates/woodpecker.yml）
FROM python:3.11-alpine

# PyYAML 用于解析仓库级配置 .github-sync.yml（缺失时脚本自带内置回退解析器）
RUN pip install --no-cache-dir pyyaml

# 安装同步脚本到 PATH
COPY gitea_github_sync.py /usr/local/bin/gitea_github_sync.py
RUN chmod +x /usr/local/bin/gitea_github_sync.py

# 默认工作目录（Woodpecker 挂载源码的位置）
WORKDIR /workspace
