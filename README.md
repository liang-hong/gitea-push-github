# Gitea → GitHub 自动推送镜像管理（Woodpecker CI）

基于 **Gitea + GitHub + Woodpecker CI** 的自动化方案：

- 本机（Ubuntu 20.04）同时运行 Gitea 与 Woodpecker CI；
- `provision` 工具扫描 Gitea 上的仓库，为缺少 `.woodpecker.yml` 的仓库**自动注入**同步流水线模板（模板默认**不**同步）；
- 需要同步的仓库在根目录添加一份 `.github-sync.yml` 声明启用；GitHub 云端仓库默认**私有**；
- 之后每次 `git push` 到 Gitea，Woodpecker CI 自动确保 GitHub 有对应镜像仓库、并配置好 Gitea 的 **Push Mirror**，由 Gitea 自身的 Push Mirror 机制把每次推送自动转发到 GitHub。

> 本仓库即该方案的实现：说明文档、部署编排、注入工具、同步工具、流水线模板与单元测试。需求背景见 [`CONTEXT.md`](CONTEXT.md)。**当前尚未部署**，请先阅读本文并在测试环境验证后再上线。

## 目录

1. [背景与目标](#1-背景与目标)
2. [整体架构](#2-整体架构)
3. [工作原理与默认行为](#3-工作原理与默认行为)
4. [仓库结构](#4-仓库结构)
5. [部署 Woodpecker CI](#5-部署-woodpecker-ci)
6. [准备本地凭据文件](#6-准备本地凭据文件)
7. [构建并推送同步工具镜像](#7-构建并推送同步工具镜像)
8. [注入流水线模板（provision 工具）](#8-注入流水线模板provision-工具)
9. [启用同步（.github-sync.yml）](#9-启用同步github-syncymml)
10. [同步工具说明](#10-同步工具说明)
11. [测试场景](#11-测试场景)
12. [本地测试](#12-本地测试)
13. [安全注意事项](#13-安全注意事项)
14. [未来扩展](#14-未来扩展)
15. [参考资料](#15-参考资料)

## 1. 背景与目标

**手动流程的痛点**：每新建一个仓库，都要在 GitHub 手工建库、进入 Gitea 仓库设置、填写 Push Mirror 地址并粘贴 Token，重复且易错。

**目标流程**：

| 步骤 | 操作 |
| ---- | ---- |
| 1 | 在 Gitea 创建仓库 |
| 2 | 运行 `provision` 工具，为缺失 `.woodpecker.yml` 的仓库自动注入流水线模板（默认不同步） |
| 3 | 需要同步的仓库添加 `.github-sync.yml` 并设置 `github.enabled: true`（默认私有） |
| 4 | `git push` 到 Gitea |
| 5 | Woodpecker CI 自动：检查 GitHub 仓库 → 不存在则创建（默认私有）→ 检查 Gitea Push Mirror → 不存在则创建 |
| 6 | 之后每次 push，Gitea Push Mirror 自动同步到 GitHub |

**默认行为（安全优先）**：

- 所有仓库默认**不**同步；只有显式在 `.github-sync.yml` 中启用（`github.enabled: true`）的仓库才会同步；
- 被创建到 GitHub 的云端仓库默认**私有**（`private: true`），需要公开时在 `.github-sync.yml` 中显式设置。

## 2. 整体架构

```text
开发者 PC / 定时任务
   │
   ├─── provision 工具（一次性/定期）：扫描 Gitea 仓库，注入 .woodpecker.yml
   │
   └─── git push
        ▼
Gitea（本地主仓库）
   │  Webhook
   ▼
Woodpecker Server ──► Woodpecker Agent/Runner
   │                      │  只读挂载本地凭据文件
   │                      ▼
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
| GitHub | 可选的云端备份 / 分发镜像（默认私有） |
| Woodpecker Server | 接收 Gitea Webhook、调度流水线 |
| Woodpecker Agent | 以 Docker 方式执行流水线步骤 |
| provision 工具（本仓库 `provision/`）| 为缺失 `.woodpecker.yml` 的仓库自动注入流水线模板 |
| 同步工具（本仓库 `sync/`）| GitHub 仓库与 Gitea Push Mirror 的检查/创建（幂等） |
| 本地凭据文件（服务器上，不在仓库中）| 集中存放 GitHub PAT 与 Gitea API Token |

## 3. 工作原理与默认行为

**触发时机**：只要仓库包含 `.woodpecker.yml` 且在 Woodpecker 中处于激活状态，`git push` 到 Gitea 后 Woodpecker 即触发一次同步流水线。

**默认不同步**：流水线中的同步工具会先读取仓库根目录的 `.github-sync.yml`；若文件缺失或 `github.enabled != true`，直接打印“跳过”并**以退出码 0 结束**，不产生任何外部调用。

**同步逻辑（幂等）**：启用后每次触发都执行“检查并确保镜像存在”：

1. 从 Woodpecker 内置变量取得仓库所有者与名称；
2. 读取本地凭据文件（只读挂载，见第 6 节）；
3. 调用 GitHub API，查询目标仓库是否存在；
4. 不存在则调用 GitHub API 创建仓库（可见性取 `.github-sync.yml` 的 `private`，**默认私有**）；
5. 调用 Gitea API，查询该仓库的 Push Mirror 列表；
6. 不存在指向 GitHub 的 Push Mirror 则自动创建（开启 `sync_on_commit`，并设 8 小时定时兜底）；
7. 全部就绪后以退出码 0 结束。

幂等性：重复运行不会产生重复仓库或重复 Mirror；任何一步失败都会以非零码退出并留下可读日志，方便在 Woodpecker 界面排查。

**声明式配置**：仓库行为完全由其配置决定（`.woodpecker.yml` 是否存在、`.github-sync.yml` 中的 `enabled` / `private`），无需改动同步工具与流水线模板本身。

## 4. 仓库结构

```text
gitea-push-github/
├── CONTEXT.md                       # 需求背景与方案演进文档
├── README.md                        # 本说明文档
├── .gitignore
├── .woodpecker.yml                  # 本仓库自身的流水线（手工复制示例）
├── deploy/
│   └── docker-compose.yml           # Woodpecker Server + Agent 部署编排
├── templates/
│   └── woodpecker.yml               # 注入到目标仓库的流水线模板（含占位符）
├── provision/
│   └── add_woodpecker_yml.py        # 注入工具：给缺失 .woodpecker.yml 的仓库自动添加
├── sync/
│   ├── gitea_github_sync.py         # 同步工具（GitHub API + Gitea API 胶水逻辑）
│   └── Dockerfile                   # 同步工具镜像构建文件
├── examples/
│   └── github-sync.yml              # 仓库级同步配置示例（默认不同步、默认私有）
└── tests/
    ├── test_sync.py                 # 同步工具单元测试
    └── test_provision.py            # 注入工具单元测试
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

### 5.3 激活仓库并开启 Trusted

1. 登录 Woodpecker → 通过 Gitea 账号授权；
2. 将 Gitea 仓库加入 Woodpecker（Activate）；
3. 在仓库的 **Project Settings** 中开启 **Trusted**：本方案的流水线通过 Docker 卷挂载只读注入本地凭据文件，而 **Woodpecker 仅在“受信任仓库”上允许卷挂载**（详见 [Volumes 文档](https://woodpecker-ci.org/docs/usage/volumes)）。请仅对本方案的受控仓库开启。

> 提示：若 Gitea 尚未启用镜像功能，请确认 `app.ini` 中 `[mirror] ENABLED = true`（默认开启）。

## 6. 准备本地凭据文件

GitHub PAT 与 Gitea API Token **只保存在服务器本地固定位置**，绝不写入任何仓库文件、`.woodpecker.yml` 或源码。

### 6.1 文件位置与权限

```bash
mkdir -p ~/.config/gitea-push-github
chmod 700 ~/.config/gitea-push-github
touch ~/.config/gitea-push-github/gitea-push-github.env
chmod 600 ~/.config/gitea-push-github/gitea-push-github.env
```

### 6.2 文件内容（KEY=VALUE，支持 `#` 注释）

```bash
# ---- GitHub 云端账号 ----
GITHUB_USERNAME=octocat
GITHUB_TOKEN=github_pat_xxxx

# ---- 本地 Gitea ----
GITEA_API_URL=https://git.example.com
GITEA_TOKEN=xxxx

# ---- 流水线模板注入参数 ----
SYNC_IMAGE=ghcr.io/octocat/gitea-github-sync:latest   # 已构建的同步工具镜像
SECRETS_MOUNT=/home/octocat/.config/gitea-push-github # 宿主机凭据目录（只读挂载）
```

说明：

- `SECRETS_MOUNT` 即本文件所在目录的宿主机绝对路径，流水线会把它只读挂载到容器 `/run/secrets/gitea-push-github`；
- `SECRETS_MOUNT` 与 `SYNC_IMAGE` 仅供 provision 工具做模板占位符替换；若省略，`SECRETS_MOUNT` 缺省取凭据文件所在目录，`SYNC_IMAGE` 必须提供；
- provision 工具与同步脚本都从这里读取配置，二者使用同一份文件，避免重复维护。

### 6.3 Token 权限

- **GitHub Token**：经典 PAT 勾选 `repo`（读写仓库）即可；Fine-grained Token 的 Repository permission 勾选 **Administration**（创建仓库需要 write）与 **Contents**（push 需要 write）；
- **Gitea Token**：Gitea **设置 → 应用 → 生成新 Token**，勾选 `write:repository`（覆盖检查与创建 Push Mirror 所需的最小权限）。

> 兼容旧方案：同步脚本也支持从进程环境变量读取凭据（Woodpecker Secrets 方式），但本方案推荐使用本地凭据文件。


## 7. 构建并推送同步工具镜像

同步流水线使用自定义镜像，需先构建并推送到镜像仓库（例如 GitHub Container Registry）：

```bash
cd sync
docker build -t ghcr.io/<your-account>/gitea-github-sync:latest .
docker push ghcr.io/<your-account>/gitea-github-sync:latest
```

镜像地址写入本地凭据文件的 `SYNC_IMAGE`（见第 6 节），provision 工具注入模板时自动替换。

## 8. 注入流水线模板（provision 工具）

provision 工具会扫描指定 Gitea 用户的所有仓库，为**缺少 `.woodpecker.yml`** 的仓库自动创建该文件（通过 Gitea API 提交，幂等）。默认跳过：

- 已存在 `.woodpecker.yml` 的仓库；
- 空仓库（无默认分支，无法通过 API 创建文件）；
- 归档仓库、镜像仓库。

### 8.1 首次预览（强烈建议）

```bash
python3 provision/add_woodpecker_yml.py --owner <gitea-owner> --dry-run
```

### 8.2 正式注入

```bash
python3 provision/add_woodpecker_yml.py --owner <gitea-owner> --exclude gitea-push-github
```

常用参数：

| 参数 | 说明 |
| ---- | ---- |
| `--owner` | Gitea 仓库所有者（用户名），必填 |
| `--credentials` | 本地凭据文件路径，默认 `~/.config/gitea-push-github/gitea-push-github.env` |
| `--template` | 模板路径，默认 `templates/woodpecker.yml` |
| `--repo` | 仅处理指定仓库（可多次传入） |
| `--exclude` | 排除指定仓库（可多次传入），建议排除本控制仓库 |
| `--sync-image` / `--secrets-mount` | 覆盖模板占位符（缺省取本地凭据文件） |
| `--dry-run` | 仅预览，不创建 |
| `--commit-message` | 注入文件的提交信息（默认符合 Conventional Commits） |

注入完成后，仓库会出现一次新的提交（如 `ci: add woodpecker sync template`）。该提交本身不会触发同步（`when: event: push` 仅对 push 事件生效，且此时尚未启用同步）。

## 9. 启用同步（.github-sync.yml）

注入的 `.woodpecker.yml` 默认**不**同步。要启用某个仓库的同步，在该仓库根目录添加 `.github-sync.yml`（参考 `examples/github-sync.yml`）：

```yaml
# 仓库级 Gitea -> GitHub 同步配置
github:
  enabled: true    # true 才启用同步；默认 false
  private: true    # GitHub 云端仓库可见性（创建时生效）；默认 true（私有）
```

**默认值**：

- 缺少本文件，或 `github.enabled` 非 `true` → **不**同步；
- `github.private` 缺省为 `true`（私有）；需要公开时显式写 `private: false`。

**启用步骤**：

```bash
# 在需要同步到 GitHub 的 Gitea 仓库根目录
cp <本仓库>/examples/github-sync.yml .github-sync.yml
# 编辑 .github-sync.yml，将 enabled 改为 true（并按需调整 private）
git add .github-sync.yml
git commit -m "ci: enable github mirror sync"
git push origin main
```

push 后到 Woodpecker 界面查看流水线：GitHub 上应出现同名仓库（默认私有），Gitea 仓库设置中应出现指向 GitHub 的 Push Mirror；再 push 一次即可看到 GitHub 镜像更新。

> 注意：`.github-sync.yml` 中的 `private` 只在 **GitHub 仓库首次创建**时生效；已存在的仓库可见性不会被修改。同步启用对既有公开仓库也是安全的（不改变其可见性）。


## 10. 同步工具说明

`sync/gitea_github_sync.py` 是唯一的自定义“胶水逻辑”，实现 GitHub 镜像供给。

**输入**：仓库所有者与名称（命令行参数，缺省时读取 Woodpecker 内置变量 `CI_REPO_OWNER` / `CI_REPO_NAME`）；仓库级配置来自工作区 `.github-sync.yml`；凭据来自本地凭据文件（优先级：`--credentials` 指定文件 → `GITEA_PUSH_GITHUB_CREDENTIALS` 环境变量指定文件 → 默认 `/run/secrets/gitea-push-github/gitea-push-github.env` → 进程环境变量）。

**执行流程（启用同步时）**：

| 步骤 | 动作 | 幂等判定 |
| ---- | ---- | -------- |
| 1 | 读取 `.github-sync.yml` | 缺失或 `enabled != true` → 跳过（退出码 0） |
| 2 | GitHub `GET /repos/{owner}/{repo}` | 200 已存在，跳过 |
| 3 | GitHub `POST /user/repos` | 仅当第 2 步返回 404 时执行；`private` 取配置（默认 `true`） |
| 4 | Gitea `GET /repos/{owner}/{repo}/push_mirrors` | 列表含目标地址即视为已存在 |
| 5 | Gitea `POST /repos/{owner}/{repo}/push_mirrors` | 仅当第 4 步未命中时执行 |

**Push Mirror 参数**：`remote_address = https://github.com/{github_username}/{repo}.git`，`remote_username` 为 GitHub 用户名，`remote_password` 为 GitHub Token，`sync_on_commit = true`，`interval = 8h0m0s`（定时兜底）。

**退出码**：`0` 成功（含“未启用同步”的跳过）；`1` 缺少必要配置、API 调用失败或创建失败（stderr 输出原因）。

## 11. 测试场景

| 场景 | 预期结果 |
| ---- | -------- |
| 新 Gitea 仓库（GitHub 无同名仓库） | GitHub 自动建库（默认私有），Push Mirror 自动创建 |
| 仓库未添加 `.github-sync.yml` | 流水线跳过，不产生任何 GitHub 调用 |
| `.github-sync.yml` 中 `enabled: false` | 同上，跳过 |
| GitHub 已有同名仓库 | 跳过创建，仅配置 Push Mirror |
| 已存在 Push Mirror | 跳过，不产生重复 |
| Mirror 被删除后再次 push | 检测缺失并自动重建 |
| 私有 / 公开仓库 | 由 `github.private` 控制创建时的 `private` 字段，默认私有 |
| 仓库未包含 `.woodpecker.yml` | 不触发任何同步 |
| provision 对已有 `.woodpecker.yml` 的仓库 | 跳过，不重复注入 |
| provision 对空/归档/镜像仓库 | 跳过 |

## 12. 本地测试

无需真实凭据与网络，单元测试通过 mock HTTP 覆盖同步与注入逻辑：

```bash
python3 -m unittest discover -s tests -v
```

若需本地端到端验证“未启用同步”的跳过行为：

```bash
tmp=$(mktemp -d)
printf 'github:\n  enabled: false\n' > "$tmp/.github-sync.yml"
python3 sync/gitea_github_sync.py --repo-owner alice --repo-name repo \
  --config "$tmp/.github-sync.yml"
echo "exit=$?"
```

## 13. 安全注意事项

- **凭据本地化**：GitHub PAT 与 Gitea API Token 只保存在服务器本地 `~/.config/gitea-push-github/gitea-push-github.env`（权限 600），通过流水线只读卷挂载注入容器；脚本不打印、不落盘、不进仓库；
- `.gitignore` 已忽略 `*.env` / `.env`，防止误提交；请勿在仓库内创建任何含 Token 的文件；
- 遵循最小权限：GitHub Token 仅授予本方案所需 scope，Gitea Token 仅 `write:repository`；
- 同步工具镜像应私有托管，避免被他人误用；
- Woodpecker **卷挂载仅在“受信任仓库”可用**：请仅对本方案的受控仓库开启 Trusted，且不要对外开放不可信的 PR（Secrets/挂载默认不对 `pull_request` 事件暴露，保持默认即可）；
- 注入模板会在目标仓库留下提交，建议 provision 使用专用账号或至少使用最小权限 Token，并在执行前先 `--dry-run` 预览。

## 14. 未来扩展

- 仓库级更多配置（如 `.github-sync.yml` 扩展 `github.repo` 重命名、多目标账号等）；
- provision 工具集成到本仓库 CI 或 cron，定期扫描新仓库自动注入；
- GitHub Release 自动创建、Docker 镜像发布、PX4 固件构建、ROS 构建验证等 CI 步骤（本方案的分层设计使其可平滑叠加）；
- 多账号 / 组织支持（将创建接口从 `/user/repos` 扩展为 `/orgs/{org}/repos`）。

## 15. 参考资料

- [Woodpecker CI 官方文档](https://woodpecker-ci.org/docs/intro)
- [Woodpecker Volumes 文档（Trusted 要求）](https://woodpecker-ci.org/docs/usage/volumes)
- [Woodpecker Secrets 文档](https://woodpecker-ci.org/docs/usage/secrets)
- [Gitea API 文档](https://docs.gitea.com/api/1.27)
- [GitHub REST API](https://docs.github.com/en/rest)
- [Gitea 官方二进制安装](https://docs.gitea.com/zh-cn/installation/install-from-binary)

