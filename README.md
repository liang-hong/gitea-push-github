# Gitea → GitHub 自动推送镜像管理（零 CI 方案）

基于 **Gitea 官方 Push Mirror + GitHub 官方 API** 的自动化备份方案，**不需要任何 CI/CD 组件**：

- **持续性同步**：Gitea 原生 Push Mirror（`sync_on_commit`）负责，配置好后每次 `git push` 到 Gitea 即时自动同步到 GitHub，并带定时兜底；
- **初始化自动化**：本仓库的 `sync/gitea_github_sync.py`（幂等）在服务器上运行，通过 **cron 定期全量扫描** 或 **Gitea post-receive hook** 为新仓库自动完成“GitHub 建库 + 配置 Push Mirror”；
- **凭据本地化**：GitHub PAT 与 Gitea API Token 只保存在服务器本地 `~/.config/gitea-push-github/gitea-push-github.env`（权限 600），绝不写入任何仓库。

> 为什么不需要 CI：镜像同步本身完全由 Gitea Push Mirror 完成；脚本只做幂等的“初始化/状态收敛”，cron 或 post-receive hook 足够，无需常驻服务。详见[与 CI 方案的对比](#7-与-ci-方案的对比)。
>
> 本仓库即该方案的实现：说明文档、同步工具、hook/cron 模板与单元测试。需求背景与演进见 [`CONTEXT.md`](CONTEXT.md)。**当前尚未部署**，请先阅读本文并在测试环境验证后再上线。

## 目录

1. [背景与目标](#1-背景与目标)
2. [整体架构](#2-整体架构)
3. [工作原理与默认行为](#3-工作原理与默认行为)
4. [仓库结构](#4-仓库结构)
5. [部署与使用](#5-部署与使用)
6. [同步工具说明](#6-同步工具说明)
7. [与 CI 方案的对比](#7-与-ci-方案的对比)
8. [测试场景](#8-测试场景)
9. [本地测试](#9-本地测试)
10. [安全注意事项](#10-安全注意事项)
11. [参考资料](#11-参考资料)

## 1. 背景与目标

**手动流程的痛点**：每新建一个仓库，都要在 GitHub 手工建库、进入 Gitea 仓库设置、填写 Push Mirror 地址并粘贴 Token，重复且易错。

**目标流程**：

| 步骤 | 操作 |
| ---- | ---- |
| 1 | 在 Gitea 创建仓库，按需在根目录添加 `.github-sync.yml`（默认不受管理） |
| 2 | `git push` 到 Gitea |
| 3 | cron 扫描或 post-receive hook 触发：按配置执行（建 GitHub 库 / 配置 Push Mirror / 停用） |
| 4 | 之后每次 push，Gitea Push Mirror 自动同步到 GitHub（无需任何 CI） |

**默认行为（安全优先）**：

- 所有仓库默认**不受管理**（`state` 缺省等同 `remove`：不创建也不删除）；只有显式 `github.state: enable` 的仓库才会同步；
- GitHub 云端仓库默认**私有**（`private: true`），需要公开时显式设置 `private: false`。

## 2. 整体架构

```text
开发者 PC
   │  git push
   ▼
Gitea（本地主仓库）
   │
   ├── post-receive hook（可选）：push 后即时调用 sync 脚本
   │
   └── Push Mirror（sync_on_commit，Gitea 原生）── 每次 push 即时同步
         │
         ▼
      GitHub 云端仓库（默认私有）

服务器定时任务（可选）：
  cron 每 10 分钟 ──► sync/gitea_github_sync.py 全量扫描（幂等）
                        │  GitHub 官方 API：检查/创建/收敛仓库
                        │  Gitea 官方 API：检查/创建/删除 Push Mirror
                        └─ 凭据只读自 ~/.config/gitea-push-github/gitea-push-github.env
```

| 组件 | 职责 |
| ---- | ---- |
| Gitea | 本地主仓库，推送入口，Push Mirror 的执行方（同步主干） |
| GitHub | 云端备份 / 分发镜像（默认私有） |
| sync 脚本（`sync/`）| 幂等状态收敛：建 GitHub 库 + 配 Push Mirror + 删 Mirror + 收敛可见性 |
| cron / post-receive hook | 初始化触发源（无需常驻服务） |
| 本地凭据文件 | 集中存放 GitHub PAT 与 Gitea API Token（服务器上，不在仓库中） |

## 3. 工作原理与默认行为

**同步主干**：配置好 Push Mirror 的仓库，Gitea 在每次 push 后立即同步（`sync_on_commit`），并默认每 8 小时兜底一次。这是 Gitea 原生能力，与任何 CI 无关。

**状态收敛（幂等 ensure）**：脚本对单个仓库执行：

1. 读取仓库主目录 `.github-sync.yml`（固定位置/文件名，经 Gitea API）；
2. 多分支仓库：仅当全部分支都有该文件且内容一致（忽略注释）时才执行策略，否则按 `disable` 处理（不报错）；
3. `state: enable` → 检查/创建 GitHub 仓库并**收敛可见性**（PATCH），检查/创建 Push Mirror；
4. `state: suspend` → 删除指向本方案 GitHub 仓库的 Push Mirror（保留 GitHub 仓库，停止更新）；
5. `state: remove` / `disable`（缺省）→ 不创建也不删除任何内容。

**触发方式**（任选或并用）：
- cron 全量扫描：新仓库最多延迟一个 cron 周期；
- post-receive hook：push 后即时处理该仓库。

**声明式配置**：仓库行为完全由 `.github-sync.yml` 的 `state` / `private` 决定，无需改动脚本。

## 4. 仓库结构

```text
gitea-push-github/
├── CONTEXT.md                       # 需求背景与方案演进文档
├── README.md                        # 本说明文档
├── .gitignore
├── sync/
│   └── gitea_github_sync.py         # 幂等状态收敛工具（GitHub 官方 API + Gitea 官方 API）
├── templates/
│   └── post-receive.sh              # Gitea post-receive hook 模板（按需即时处理）
├── examples/
│   ├── github-sync.yml              # 仓库级同步配置示例（默认不受管理）
│   └── crontab.txt                  # cron 定期全量扫描示例
└── tests/
    └── test_sync.py                 # 同步工具单元测试（mock HTTP）
```

## 5. 部署与使用

部署与后续修改的所有步骤都在本章，按顺序执行即可。

### 5.1 准备本地凭据文件

GitHub Token 与 Gitea API Token **只保存在服务器本地固定位置**，绝不写入任何仓库。申请步骤见下文「5.3 申请 Token」。

```bash
mkdir -p ~/.config/gitea-push-github
chmod 700 ~/.config/gitea-push-github
touch ~/.config/gitea-push-github/gitea-push-github.env
chmod 600 ~/.config/gitea-push-github/gitea-push-github.env
```

文件内容（`KEY=VALUE`，支持 `#` 注释）：

```bash
# ---- GitHub 云端账号 ----
GITHUB_USERNAME=octocat
GITHUB_TOKEN=github_pat_xxxx

# ---- 本地 Gitea ----
GITEA_API_URL=https://git.example.com
GITEA_TOKEN=xxxx
```

### 5.2 仓库内同步配置 `.github-sync.yml`

每个仓库在**主目录**放一个 `.github-sync.yml`（固定文件名/位置，其他位置无效）声明自己的推送策略：

```yaml
# 仓库级 Gitea -> GitHub 同步配置
github:
  state: disable   # enable / suspend / remove / disable；默认 disable（等同 remove）
  private: true    # GitHub 云端仓库可见性（创建时生效，之后每次收敛也生效）；默认 true
  default_branch: no-ci  # 可选；同时设置 Gitea 与 GitHub 云端仓库默认分支（未配置则不改动）
```

`state` 语义：

| state | 行为 |
| ---- | ---- |
| `enable` | 创建/补齐：GitHub 无同名仓库则创建（默认私有）；已存在则按配置**收敛可见性**（PATCH）；Push Mirror 缺失则创建（保留已存在者） |
| `suspend` | 停止更新：删除指向本方案 GitHub 仓库的 Gitea Push Mirror；GitHub 仓库保留不删 |
| `remove` / `disable` | 不受管理：不创建也不删除 GitHub 仓库 / Push Mirror（二者等同） |

规则：

- 缺少本文件、无 `github` 段或未写 `state` → 等同 `disable`：不创建/删除任何内容、**不报错**；
- `state` 为其他任何值（如 `foo` / `true`）→ **脚本报错，退出码 1**；
- **多分支仓库**：仅当**全部分支**都有 `.github-sync.yml` 且内容一致（忽略注释）时才执行策略；任一分支缺失或不一致 → 按 `disable` 处理（不报错）；
- 可见性：公开仓库 + 配置 `private: true` → 自动改回私有；私有仓库改公开需显式 `private: false`，脚本会打印警告；
- `default_branch`（可选）：**同时设置 Gitea 与 GitHub** 云端仓库默认分支；仅 `state=enable` 时生效；GitHub 侧需该分支已同步到 GitHub（否则本次跳过并警告，镜像同步后下次运行生效）。分支参数为空时按 git 常见默认主分支名处理（优先 `main`，其次 `master`）。

### 5.3 申请 Token（GitHub / Gitea）

#### 5.3.1 GitHub：Fine-grained Token

操作步骤：

1. 登录 GitHub → 右上角头像 → **Settings**；
2. 左下角 **Developer settings** → **Personal access tokens** → **Fine-grained tokens**；
3. 点 **Generate new token**；
4. 填写字段：
   - **Token name**：如 `gitea-push-github`；
   - **Expiration**：建议较短（如 90 天，到期后到本文件更新 Token）；
   - **Resource owner**：选你自己的账户；
   - **Repository access**：选 **All repositories**——本方案会为以后**新建**的仓库在 GitHub 建库，选 Selected repositories 则新建仓库不在访问范围内，需手动逐个添加；
5. **Repository permissions** 勾选：
   - **Administration** → **Read and write**（创建仓库、修改可见性必需）；
   - **Contents** → **Read and write**（向 GitHub push 代码必需；Metadata read 自动附带）；
6. 点 **Generate token** → **立即复制**：令牌**只显示一次**，关闭页面后无法再查看。

注意：

- 仓库不在 Fine-grained Token 访问范围内时，API 会返回 404/403，脚本按对应状态报错；
- 权限不足（如 Administration 缺 write）时，创建仓库/修改可见性会失败，脚本打印 HTTP 错误并退出 1。

#### 5.3.2 Gitea：API Token

操作步骤：

1. 登录 Gitea → 右上角头像 → **设置（Settings）**；
2. 左侧菜单 **应用（Applications）**；
3. 「生成新令牌（Generate New Token）」区块，填写令牌名称（如 `gitea-push-github`）；
4. 权限勾选 **repository：write**（即 `write:repository`）——检查/创建/删除 Push Mirror 所需的最小权限；
5. 点 **生成令牌** → **立即复制**：令牌**只显示一次**。

### 5.4 初始化自动化（二选一或并用）

**方式一：cron 全量扫描**（推荐默认）

```bash
# crontab -e 追加（见 examples/crontab.txt；替换 <owner> 与脚本绝对路径）
*/10 * * * * /usr/bin/python3 /path/to/gitea-push-github/sync/gitea_github_sync.py --repo-owner <owner> >> /var/log/gitea-push-github.log 2>&1
```

新仓库创建后最多延迟一个 cron 周期（建议 5–15 分钟）自动完成；幂等，重复运行无副作用；空/归档/镜像仓库自动跳过。

**方式二：post-receive hook**（push 后即时处理）

在目标仓库 Gitea **设置 → Git Hooks → post-receive** 粘贴 `templates/post-receive.sh` 并替换占位符：

```bash
exec /path/to/gitea-push-github/sync/gitea_github_sync.py \
  --repo-owner "$GITEA_REPO_USER_NAME" \
  --repo-name "$GITEA_REPO_NAME" \
  --credentials /home/<user>/.config/gitea-push-github/gitea-push-github.env \
  >> /var/log/gitea-push-github.log 2>&1
```

> 先执行 `--dry-run` 预览：`python3 sync/gitea_github_sync.py --repo-owner <owner> --dry-run`（单仓库加 `--repo-name`），只打印将执行的动作，不实际创建/删除/修改。

### 5.5 启用 / 修改 / 停用

所有操作都在目标仓库主目录改 `.github-sync.yml` 并 push 即可（无需改脚本）：

```bash
# 启用同步：state: enable（并按需调整 private）
git commit -m "chore: set github sync state to enable"
git push origin main
```

- **启用**：`state: enable` → push 后（hook/cron）GitHub 自动建库（默认私有）、Push Mirror 自动配置、可见性收敛；
- **停止更新**：`state: suspend` → push 后脚本删除该仓库的 Gitea Push Mirror，GitHub 仓库保留不删；
- **退出管理**：`state: remove`（或 `disable`，或删除该文件）→ 已有 GitHub 仓库与 Push Mirror 均不会被删除或修改，脚本不报错；
- **修改可见性**：改 `private` 后 push，`enable` 状态下脚本会 PATCH 收敛；
- **验证**：查看脚本日志（`/var/log/gitea-push-github.log`）与 Gitea 仓库“推送镜像”设置页。

## 6. 同步工具说明

`sync/gitea_github_sync.py` 是唯一的自定义逻辑，实现“GitHub 镜像”的幂等状态收敛。

**输入**：`--repo-owner`（缺省取 `GITEA_REPO_USER_NAME` / `CI_REPO_OWNER`）、可选 `--repo-name`（缺省遍历全部仓库）、可选 `--credentials` 与 `--dry-run`。

**执行流程**（`state=enable` 时）：

| 步骤 | 动作 | 幂等判定 |
| ---- | ---- | -------- |
| 1 | 读取各分支 `.github-sync.yml` | 缺失/不一致 → 等同 `disable`（不报错）；`state` 非法 → 报错（退出码 1） |
| 2 | GitHub `GET /repos/{owner}/{repo}` | 200 已存在：`private` 与配置不一致 → PATCH 收敛；404 进入创建 |
| 3 | GitHub `POST /user/repos` | 仅当第 2 步返回 404 时执行；`private` 取配置（默认 `true`） |
| 4 | GitHub `PATCH /repos/{owner}/{repo}` | 仅当第 2 步 200 且可见性或默认分支与配置不一致时执行 |
| 5 | Gitea `PATCH /repos/{owner}/{repo}` | 配置了 `default_branch` 且与 Gitea 当前默认分支不一致时执行 |
| 6 | Gitea `GET .../push_mirrors` | 列表含目标地址即视为已存在 |
| 7 | Gitea `POST .../push_mirrors` | 仅当第 6 步未命中时执行 |

**Push Mirror 参数**：`remote_address = https://github.com/{github_username}/{repo}.git`，`sync_on_commit = true`，`interval = 8h0m0s`（定时兜底）。

**`state=suspend` 时**：Gitea `DELETE .../push_mirrors/{remote_name}`，仅删除 `remote_address` 精确匹配的 Push Mirror；GitHub 仓库不动。

**退出码**：`0` 成功（含 `remove/disable` 跳过）、`1` 失败（含 `state` 非法）；全量模式汇总所有仓库，存在失败时返回 `1`。

## 7. 与 CI 方案的对比

| 维度 | 本方案（零 CI） | CI 方案（Woodpecker / Gitea Actions） |
| ---- | ---- | ---- |
| 持续性同步 | Gitea Push Mirror 原生（即时 + 定时兜底） | 同样依赖 Gitea Push Mirror 或镜像推送 |
| 初始化触发 | cron 扫描 / post-receive hook | CI 在 push 事件后运行 |
| 常驻组件 | 无（cron 一行） | CI server/agent/runner |
| 失败可见性 | Gitea“推送镜像”设置页 + 脚本日志 | CI 流水线日志 |
| 自研代码 | 单个幂等脚本 | 脚本 + workflow 模板 |
| 运维成本 | 最低 | 较高 |

## 8. 测试场景

| 场景 | 预期结果 |
| ---- | -------- |
| 新仓库，`state: enable` | GitHub 自动建库（默认私有），Push Mirror 自动创建 |
| 仓库无 `.github-sync.yml` | 等同 `disable`：不产生任何 GitHub 调用，已有 Push Mirror 不动，不报错 |
| `state: remove` / `disable` | 同上，跳过 |
| `state: suspend` | 删除匹配的 Gitea Push Mirror，保留 GitHub 仓库 |
| 多分支：任一分支缺文件或内容不一致 | 按 `disable` 处理，不报错 |
| 多分支：全部分支一致（仅注释不同） | 正常执行策略 |
| `state` 为非法值（如 `foo` / `true`） | 报错，退出码 1 |
| 已存在仓库可见性与配置不一致（`enable`） | PATCH 收敛可见性；公开改私有自动，私有改公开需显式 `private: false` 并打印警告 |
| 配置 `default_branch`（分支已同步） | PATCH 设置 Gitea 与 GitHub 默认分支 |
| 配置 `default_branch`（分支尚未同步） | GitHub 侧本次跳过并警告；Gitea 侧照常设置，镜像同步后下次运行生效 |
| `--dry-run` | 只打印将执行动作，不实际创建/删除/修改 |

## 9. 本地测试

无需真实凭据与网络，单元测试通过 mock HTTP 覆盖全部逻辑：

```bash
python3 -m unittest discover -s tests -v
```

## 10. 安全注意事项

- **凭据本地化**：GitHub PAT 与 Gitea API Token 只保存在服务器本地 `~/.config/gitea-push-github/gitea-push-github.env`（权限 600）；脚本不打印、不落盘、不进仓库；
- `.gitignore` 已忽略 `*.env` / `.env`，防止误提交；
- 遵循最小权限：GitHub Token 仅授予本方案所需 scope，Gitea Token 仅 `write:repository`；
- post-receive hook 内容存于 Gitea 数据库（非仓库文件）；建议使用绝对路径与日志重定向；
- 非破坏性方向：脚本只创建缺失的 GitHub 仓库与 Mirror、按配置收敛可见性、按 `suspend` 精确删除匹配 Mirror；`remove/disable` 及缺失配置一律不动已有内容。

## 11. 参考资料

- [Gitea Mirror Repository（官方文档，含 push mirror 与 post-receive hook 说明）](https://docs.gitea.com/usage/repository/repo-mirror)
- [Gitea API 文档](https://docs.gitea.com/api/1.27)
- [GitHub REST API](https://docs.github.com/en/rest)
- [GitHub CLI（gh）](https://cli.github.com/manual/gh_repo_create)
- [Gitea 官方二进制安装](https://docs.gitea.com/zh-cn/installation/install-from-binary)
