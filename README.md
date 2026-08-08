# Gitea → GitHub 自动推送镜像管理（Woodpecker CI）

基于 **Gitea + GitHub + Woodpecker CI** 的自动化方案：在 Gitea 仓库中添加一份 CI 配置文件后，后续每次 `git push` 到 Gitea，Woodpecker CI 都会自动确保 GitHub 上有对应的镜像仓库、并配置好 Gitea 的 **Push Mirror**，之后由 Gitea 自身的 Push Mirror 机制把每一次推送自动转发到 GitHub。

> 本仓库即该方案的实现，包含说明文档、部署编排、同步工具与仓库级 CI 模板。需求背景见 [`CONTEXT.md`](CONTEXT.md)。

## 目录

1. [背景与目标](#1-背景与目标)
2. [整体架构](#2-整体架构)
3. [工作原理](#3-工作原理)
4. [仓库结构](#4-仓库结构)
5. [部署 Woodpecker CI](#5-部署-woodpecker-ci)
6. [构建并推送同步工具镜像](#6-构建并推送同步工具镜像)
7. [配置 Secrets](#7-配置-secrets)
8. [在目标仓库启用同步](#8-在目标仓库启用同步)
9. [同步工具说明](#9-同步工具说明)
10. [测试场景](#10-测试场景)
11. [安全注意事项](#11-安全注意事项)
12. [未来扩展](#12-未来扩展)
13. [参考资料](#13-参考资料)

## 1. 背景与目标

**手动流程的痛点**：每新建一个仓库，都要在 GitHub 手工建库、进入 Gitea 仓库设置、填写 Push Mirror 地址并粘贴 Token，重复且易错。

**目标流程**：

| 步骤 | 操作 |
| ---- | ---- |
| 1 | 在 Gitea 创建仓库 |
| 2 | 在仓库根目录添加 `.woodpecker.yml`（本方案模板），声明“需要同步” |
| 3 | `git push` 到 Gitea |
| 4 | Woodpecker CI 自动：检查 GitHub 仓库 → 不存在则创建 → 检查 Gitea Push Mirror → 不存在则创建 |
| 5 | 之后每次 push，Gitea Push Mirror 自动同步到 GitHub |

**默认行为**：所有仓库默认**不**同步，只有显式包含 `.woodpecker.yml` 的仓库才会被同步。

## 2. 整体架构

```text
开发者 PC
  │  git push
  ▼
Gitea（本地主仓库）
  │  Webhook
  ▼
Woodpecker Server ──► Woodpecker Agent/Runner
  │
  ├───────────────────────────────┐
  ▼                               ▼
GitHub API                  Gitea API
（检查/创建仓库）            （检查/创建 Push Mirror）
  │                               │
  └───────────────┬───────────────┘
                  ▼
          GitHub 镜像仓库
   （后续推送由 Gitea Push Mirror 自动转发）
```

组件说明：

| 组件 | 职责 |
| ---- | ---- |
| Gitea | 本地主仓库，推送入口，Push Mirror 的执行方 |
| GitHub | 可选的公共备份 / 分发镜像 |
| Woodpecker Server | 接收 Gitea Webhook、调度流水线、托管 Secrets |
| Woodpecker Agent | 以 Docker 方式执行流水线步骤 |
| 同步工具（本仓库 `sync/`）| 唯一需要自研的“胶水逻辑”：GitHub 仓库与 Gitea Push Mirror 的检查/创建 |

## 3. 工作原理

**触发时机**：选择“每次 push 立即触发同步”。只要仓库包含 `.woodpecker.yml`，`git push` 到 Gitea 后 Woodpecker 即触发一次同步流水线。

**同步逻辑（幂等）**：每次触发都执行“检查并确保镜像存在”：

1. 从 Woodpecker 内置变量取得仓库所有者与名称；
2. 调用 GitHub API，查询目标仓库是否存在；
3. 不存在则调用 GitHub API 创建仓库（可见性由配置决定，默认公开）；
4. 调用 Gitea API，查询该仓库的 Push Mirror 列表；
5. 不存在指向 GitHub 的 Push Mirror 则自动创建（开启 `sync_on_commit`，并设 8 小时定时兜底）；
6. 全部就绪后正常退出。

幂等性：重复运行不会产生重复仓库或重复 Mirror，任何一步失败都会以非零码退出并留下可读日志，方便在 Woodpecker 界面排查。

**声明式配置**：仓库行为完全由其配置（是否含 `.woodpecker.yml`、是否配置 `github_private` Secret）决定，无需改动同步工具本身。

## 4. 仓库结构

```text
gitea-push-github/
├── CONTEXT.md                       # 需求背景文档（原始）
├── README.md                        # 本说明文档
├── .woodpecker.yml                  # 仓库级同步流水线模板（复制到目标仓库）
├── .gitignore
├── deploy/
│   └── docker-compose.yml           # Woodpecker Server + Agent 部署编排
└── sync/
    ├── gitea_github_sync.py         # 同步工具（GitHub API + Gitea API 胶水逻辑）
    └── Dockerfile                   # 同步工具镜像构建文件
```

## 5. 部署 Woodpecker CI

Woodpecker 部署在 Gitea 所在的 Ubuntu 20.04 服务器上（前置条件：已安装 Docker）。

### 5.1 在 Gitea 创建 OAuth2 应用

1. 登录 Gitea → 右上角头像 → **设置** → **应用** → **管理 OAuth2 应用程序**；
2. 应用名称填 `Woodpecker CI`；
3. 重定向 URI 填 `http://<woodpecker-地址>/authorize`（例如 `https://woodpecker.example.com/authorize`）；
4. 保存后复制 **Client ID** 与 **Client Secret**，填入 `deploy/docker-compose.yml`。

### 5.2 修改并启动编排

```bash
cd deploy
# 编辑 docker-compose.yml：替换 Gitea 地址、OAuth Client/Secret、Agent Secret、对外地址
docker compose up -d
```

### 5.3 关联 Gitea 仓库

1. 打开 Woodpecker Web 界面，使用 Gitea 账号登录；
2. 在 **Repositories** 页面勾选要接入 CI 的仓库；
3. 对需要同步的仓库，在仓库 **Settings → Secrets** 中添加第 7 节列出的 Secrets；
4. 往 Gitea 仓库 push 代码，即可在 Woodpecker 看到流水线运行。

> 提示：若 Gitea 尚未启用镜像功能，请确认 `app.ini` 中 `[mirror] ENABLED = true`（默认开启）。

## 6. 构建并推送同步工具镜像

同步流水线使用自定义镜像，需先构建并推送到镜像仓库（例如 GitHub Container Registry）：

```bash
cd sync
docker build -t ghcr.io/<your-account>/gitea-github-sync:latest .
docker push ghcr.io/<your-account>/gitea-github-sync:latest
```

然后把 `.woodpecker.yml` 模板中的 `image` 字段替换为实际镜像地址。

## 7. 配置 Secrets

密钥只存于 **Woodpecker Secrets**（仓库级或组织级），绝不写入任何仓库文件或源码。

| Secret 名称 | 说明 | 示例 |
| ----------- | ---- | ---- |
| `github_username` | GitHub 目标账号（固定个人账号） | `octocat` |
| `github_token` | GitHub Personal Access Token（见下） | `github_pat_xxx` |
| `gitea_api_url` | Gitea 实例基础地址 | `https://git.example.com` |
| `gitea_token` | Gitea API Token（见下） | `xxxx` |
| `github_private`（可选） | 目标仓库可见性，`true` 为私有，默认 `false` 公开 | `false` |

### GitHub Token 权限

- **经典 Token（PAT）**：勾选 `repo`（读写仓库）即可；
- **Fine-grained Token**：Repository permission 中勾选 **Administration**（创建仓库需要 write）与 **Contents**（push 需要 write）。

### Gitea Token 权限

在 Gitea **设置 → 应用 → 生成新 Token**，勾选 `write:repository`（覆盖检查与创建 Push Mirror 所需的最小权限）。

## 8. 在目标仓库启用同步

```bash
# 在任意需要同步到 GitHub 的 Gitea 仓库根目录
cp <本仓库>/.woodpecker.yml .
# 确认 image 字段指向已构建的同步工具镜像（README 第 6 节）
# 确认 Woodpecker 中该仓库已配置第 7 节 Secrets
git add .woodpecker.yml
git commit -m "ci: enable github mirror sync"
git push origin main
```

push 后到 Woodpecker 界面查看流水线：GitHub 上应出现同名仓库，Gitea 仓库设置中应出现指向 GitHub 的 Push Mirror；再 push 一次即可看到 GitHub 镜像更新。

## 9. 同步工具说明

`sync/gitea_github_sync.py` 是唯一的自定义代码，仅实现“GitHub 镜像供给”这一胶水逻辑。

**输入**：仓库所有者与名称（命令行参数，缺省时读取 Woodpecker 内置变量 `CI_REPO_OWNER` / `CI_REPO_NAME`）；凭据与地址来自环境变量（由 Secrets 注入）。

**执行流程**：

| 步骤 | 动作 | 幂等判定 |
| ---- | ---- | -------- |
| 1 | GitHub `GET /repos/{owner}/{repo}` | 200 已存在，跳过 |
| 2 | GitHub `POST /user/repos` | 仅当第 1 步返回 404 时执行 |
| 3 | Gitea `GET /repos/{owner}/{repo}/push_mirrors` | 列表含目标地址即视为已存在 |
| 4 | Gitea `POST /repos/{owner}/{repo}/push_mirrors` | 仅当第 3 步未命中时执行 |

**Push Mirror 参数**：`remote_address = https://github.com/{github_username}/{repo}.git`，`remote_username` 为 GitHub 用户名，`remote_password` 为 GitHub Token，`sync_on_commit = true`，`interval = 8h0m0s`（定时兜底）。

**退出码**：`0` 成功；`1` 缺少必要配置、API 调用失败或创建失败（stderr 输出原因）。

## 10. 测试场景

| 场景 | 预期结果 |
| ---- | -------- |
| 新 Gitea 仓库（GitHub 无同名仓库） | GitHub 自动建库，Push Mirror 自动创建 |
| GitHub 已有同名仓库 | 跳过创建，仅配置 Push Mirror |
| 已存在 Push Mirror | 跳过，不产生重复 |
| Mirror 被删除后再次 push | 检测缺失并自动重建 |
| 公共 / 私有仓库 | 由 `github_private` Secret 控制创建时的 `private` 字段 |
| 仓库未包含 `.woodpecker.yml` | 不触发任何同步 |

## 11. 安全注意事项

- GitHub / Gitea Token 只存在 Woodpecker Secrets，脚本不打印、不落盘、不进仓库；
- 遵循最小权限：GitHub Token 仅授予本方案所需 scope，Gitea Token 仅 `write:repository`；
- 同步工具镜像应私有托管，避免被他人误用；
- Woodpecker 中如仓库对外开放 PR，注意 Secrets 默认不对 `pull_request` 事件暴露（保持默认即可）。

## 12. 未来扩展

- 仓库级更多配置（如 `.github-sync.yml`：`github.enabled` / `github.private`），实现完全声明式；
- GitHub Release 自动创建、Docker 镜像发布、PX4 固件构建、ROS 构建验证等 CI 步骤（本方案的分层设计使其可平滑叠加）；
- 多账号 / 组织支持（将创建接口从 `/user/repos` 扩展为 `/orgs/{org}/repos`）。

## 13. 参考资料

- [Woodpecker CI 官方文档](https://woodpecker-ci.org/docs/intro)
- [Woodpecker Secrets 文档](https://woodpecker-ci.org/docs/usage/secrets)
- [Gitea API 文档](https://docs.gitea.com/api/1.27)
- [GitHub REST API](https://docs.github.com/en/rest)
- [Gitea 官方二进制安装](https://docs.gitea.com/zh-cn/installation/install-from-binary)
