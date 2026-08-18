# Gitea → GitHub 自动推送镜像管理（零 CI 方案）

基于 **Gitea 官方 Push Mirror + GitHub 官方 API/gh CLI** 的自动化方案，**不需要任何 CI/CD 组件**：

- **持续性同步**：由 Gitea 原生 Push Mirror（`sync_on_commit`）负责——仓库配置好后，每次 `git push` 到 Gitea 都会即时自动同步到 GitHub，并带定时兜底；
- **初始化自动化**：本仓库的 `sync/gitea_github_sync.py`（幂等）在服务器上直接运行，通过 **cron 定期全量扫描** 或 **Gitea post-receive hook 按需触发**，为新仓库自动完成“GitHub 建库 + 配置 Push Mirror”；
- **凭据本地化**：GitHub PAT 与 Gitea API Token 只保存在服务器本地 `~/.config/gitea-push-github/gitea-push-github.env`（权限 600），绝不写入任何仓库。

> 为什么不需要 CI：镜像同步本身完全由 Gitea Push Mirror 完成；CI（Woodpecker 等）只做“初始化”，而初始化是幂等的一步式操作，cron 或 post-receive hook 足够，无需常驻服务。详见[本方案与 CI 方案的对比](#10-与-ci-方案的对比)。

> 本仓库即该方案的实现：说明文档、同步工具、hook/cron 模板与单元测试。需求背景与演进见 [`CONTEXT.md`](CONTEXT.md)。**当前尚未部署**，请先阅读本文并在测试环境验证后再上线。

## 目录

1. [背景与目标](#1-背景与目标)
2. [整体架构](#2-整体架构)
3. [工作原理与默认行为](#3-工作原理与默认行为)
4. [仓库结构](#4-仓库结构)
5. [准备本地凭据文件](#5-准备本地凭据文件)
6. [初始化自动化（方式一：cron 全量扫描）](#6-初始化自动化方式一cron-全量扫描)
7. [初始化自动化（方式二：post-receive hook）](#7-初始化自动化方式二post-receive-hook)
8. [启用 / 关闭同步（.github-sync.yml）](#8-启用--关闭同步github-syncymml)
9. [同步工具说明](#9-同步工具说明)
10. [与 CI 方案的对比](#10-与-ci-方案的对比)
11. [测试场景](#11-测试场景)
12. [本地测试](#12-本地测试)
13. [安全注意事项](#13-安全注意事项)
14. [参考资料](#14-参考资料)

## 1. 背景与目标

**手动流程的痛点**：每新建一个仓库，都要在 GitHub 手工建库、进入 Gitea 仓库设置、填写 Push Mirror 地址并粘贴 Token，重复且易错。

**目标流程**：

| 步骤 | 操作 |
| ---- | ---- |
| 1 | 在 Gitea 创建仓库，按需在根目录添加 `.github-sync.yml`（默认不同步） |
| 2 | `git push` 到 Gitea |
| 3 | cron 扫描或 post-receive hook 触发：GitHub 自动建库（默认私有）→ 自动配置 Gitea Push Mirror（`sync_on_commit`） |
| 4 | 之后每次 push，Gitea Push Mirror 自动同步到 GitHub（无需任何 CI） |

**默认行为（安全优先）**：

- 所有仓库默认**不**同步；只有显式在 `.github-sync.yml` 中启用（`github.enabled: true`）的仓库才会同步；
- 被创建到 GitHub 的云端仓库默认**私有**（`private: true`），需要公开时在 `.github-sync.yml` 中显式设置。

## 2. 整体架构

```text
开发者 PC
   │  git push
   ▼
Gitea（本地主仓库）
   │
   ├── post-receive hook（可选）：push 后即时调用 sync 脚本初始化
   │
   └── Push Mirror（sync_on_commit，Gitea 原生）── 每次 push 即时同步
         │
         ▼
      GitHub 云端仓库（默认私有）

服务器定时任务（可选）：
  cron 每 10 分钟 ──► sync/gitea_github_sync.py 全量扫描（幂等）
                        │  GitHub 官方 API：检查/创建仓库
                        │  Gitea 官方 API：检查/创建 Push Mirror
                        └─ 凭据只读自 ~/.config/gitea-push-github/gitea-push-github.env
```

组件说明：

| 组件 | 职责 |
| ---- | ---- |
| Gitea | 本地主仓库，推送入口，Push Mirror 的执行方（同步主干，原生能力） |
| GitHub | 云端备份 / 分发镜像（默认私有） |
| sync 脚本（本仓库 `sync/`）| 幂等初始化：GitHub 建库 + 配置 Gitea Push Mirror |
| cron / post-receive hook | 初始化触发源（无需常驻服务） |
| 本地凭据文件（服务器上，不在仓库中）| 集中存放 GitHub PAT 与 Gitea API Token |

## 3. 工作原理与默认行为

**同步主干**：配置好 Push Mirror 的仓库，Gitea 在每次 push 后立即同步（`sync_on_commit`），并默认每 8 小时兜底一次。这一步是 Gitea 原生能力，**与任何 CI 无关**。

**初始化（幂等 ensure）**：`sync` 脚本对单个仓库执行：

1. 读取该仓库的 `.github-sync.yml`（本地 `--config`，或通过 Gitea API 读取）；缺失或 `github.enabled != true` → 跳过（默认不同步）；
2. 调用 GitHub API 检查云端仓库，不存在则创建（`private` 取配置，**默认私有**）；
3. 调用 Gitea API 检查 Push Mirror，不存在则创建（`sync_on_commit=true`，间隔 `8h0m0s`）。

**触发方式**（可任选或并用）：
- **cron 全量扫描**：`sync` 脚本不带 `--repo-name` 时遍历所有仓库，幂等补齐（新仓库最多延迟一个 cron 周期）；
- **post-receive hook**：每个要同步的仓库配置一个 hook，push 后即时初始化该仓库。

**声明式配置**：仓库行为完全由其配置决定（`.github-sync.yml` 的 `enabled` / `private`），无需改动脚本本身。

## 4. 仓库结构

```text
gitea-push-github/
├── CONTEXT.md                       # 需求背景与方案演进文档
├── README.md                        # 本说明文档
├── .gitignore
├── sync/
│   └── gitea_github_sync.py         # 幂等初始化工具（GitHub 官方 API + Gitea 官方 API）
├── templates/
│   └── post-receive.sh              # Gitea post-receive hook 模板（按需即时初始化）
├── examples/
│   ├── github-sync.yml              # 仓库级同步配置示例（默认不同步、默认私有）
│   └── crontab.txt                  # cron 定期全量扫描示例
└── tests/
    └── test_sync.py                 # 同步工具单元测试（mock HTTP）
```

## 5. 准备本地凭据文件

GitHub PAT 与 Gitea API Token **只保存在服务器本地固定位置**，绝不写入任何仓库文件或源码。

### 5.1 文件位置与权限

```bash
mkdir -p ~/.config/gitea-push-github
chmod 700 ~/.config/gitea-push-github
touch ~/.config/gitea-push-github/gitea-push-github.env
chmod 600 ~/.config/gitea-push-github/gitea-push-github.env
```

### 5.2 文件内容（KEY=VALUE，支持 `#` 注释）

```bash
# ---- GitHub 云端账号 ----
GITHUB_USERNAME=octocat
GITHUB_TOKEN=github_pat_xxxx

# ---- 本地 Gitea ----
GITEA_API_URL=https://git.example.com
GITEA_TOKEN=xxxx
```

### 5.3 Token 权限

- **GitHub Token**：经典 PAT 勾选 `repo`（读写仓库）即可；Fine-grained Token 的 Repository permission 勾选 **Administration**（创建仓库需要 write）与 **Contents**（push 需要 write）；
- **Gitea Token**：Gitea **设置 → 应用 → 生成新 Token**，勾选 `write:repository`（覆盖检查与创建 Push Mirror 所需的最小权限）。

> 可选用 GitHub 官方 **gh CLI** 手动建库（`gh repo create <name> --private`，`GH_TOKEN` 指向上面的 PAT）。脚本本身使用 GitHub 官方 REST API，二者等效。

## 6. 初始化自动化（方式一：cron 全量扫描）

推荐作为默认方式：一个 cron 任务定期全量扫描，幂等补齐缺失的 GitHub 仓库与 Push Mirror。已配置好的仓库由 Push Mirror 即时同步，与 cron 间隔无关。

```bash
# crontab -e 追加（见 examples/crontab.txt）
*/10 * * * * /usr/bin/python3 /path/to/gitea-push-github/sync/gitea_github_sync.py --repo-owner <owner> >> /var/log/gitea-push-github.log 2>&1
```

特点：

- 新仓库创建后，最多延迟一个 cron 周期（建议 5–15 分钟）即自动完成初始化；
- 无需在每个仓库配置任何东西；`.github-sync.yml` 通过 Gitea API 读取，仓库根目录直接提交即可；
- 幂等：重复运行无副作用；空/归档/镜像仓库自动跳过。

## 7. 初始化自动化（方式二：post-receive hook）

若希望“push 后立即初始化”，可为目标仓库配置 Gitea **post-receive hook**（Gitea 官方文档认可的方式）。push 到 Gitea 时即调用脚本完成该仓库的初始化，之后再交给 Push Mirror 同步。

配置方法：Gitea 仓库 → **设置 → Git Hooks → post-receive**，粘贴 `templates/post-receive.sh` 并替换占位符：

```bash
exec /path/to/gitea-push-github/sync/gitea_github_sync.py \
  --repo-owner "$GITEA_REPO_USER_NAME" \
  --repo-name "$GITEA_REPO_NAME" \
  --credentials /home/<user>/.config/gitea-push-github/gitea-push-github.env \
  >> /var/log/gitea-push-github.log 2>&1
```

特点：

- push 后即时完成“GitHub 建库 + 配置 Push Mirror”，无 cron 延迟；
- hook 需要按仓库配置（也可通过 Gitea API 批量注入 git hooks）；
- 与 cron 方式可并存（hook 保证即时、cron 兜底）。

> 提示：hook 只在仓库包含 `.github-sync.yml` 且 `enabled: true` 时才实际创建 GitHub 仓库；否则脚本立即以退出码 0 跳过，不影响 push。


## 8. 启用 / 关闭同步（.github-sync.yml）

在仓库根目录添加 `.github-sync.yml`（参考 `examples/github-sync.yml`）声明是否同步：

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

push 后（cron 扫描或 hook 触发）：GitHub 上出现同名私有仓库，Gitea 仓库设置中出现指向 GitHub 的 Push Mirror；再 push 一次即可看到 GitHub 镜像更新。

> 关闭同步：把 `.github-sync.yml` 中 `enabled` 改为 `false`（或删除文件）后 push 即可；已创建的 GitHub 仓库与已配置的 Push Mirror 不会被删除（脚本只做幂等补齐，不做破坏性操作）。

## 9. 同步工具说明

`sync/gitea_github_sync.py` 是唯一的自定义逻辑，实现“GitHub 镜像供给”的幂等初始化。

**输入**：

- 仓库所有者与名称：`--repo-owner` / `--repo-name`（不传 `--repo-name` 则遍历全部仓库）；`--repo-owner` 缺省取环境变量 `GITEA_REPO_USER_NAME` / `CI_REPO_OWNER`；
- 仓库级配置：`--config` 本地文件，或缺省通过 Gitea API 读取仓库内 `.github-sync.yml`；
- 凭据：本地凭据文件（优先级：`--credentials` 指定 → `GITEA_PUSH_GITHUB_CREDENTIALS` 指定 → 默认 `~/.config/gitea-push-github/gitea-push-github.env` → 进程环境变量）。

**执行流程（启用同步时）**：

| 步骤 | 动作 | 幂等判定 |
| ---- | ---- | -------- |
| 1 | 读取 `.github-sync.yml` | 缺失或 `enabled != true` → 跳过（退出码 0） |
| 2 | GitHub `GET /repos/{owner}/{repo}` | 200 已存在，跳过 |
| 3 | GitHub `POST /user/repos` | 仅当第 2 步返回 404 时执行；`private` 取配置（默认 `true`） |
| 4 | Gitea `GET /repos/{owner}/{repo}/push_mirrors` | 列表含目标地址即视为已存在 |
| 5 | Gitea `POST /repos/{owner}/{repo}/push_mirrors` | 仅当第 4 步未命中时执行 |

**Push Mirror 参数**：`remote_address = https://github.com/{github_username}/{repo}.git`，`remote_username` 为 GitHub 用户名，`remote_password` 为 GitHub Token，`sync_on_commit = true`，`interval = 8h0m0s`（定时兜底）。

**退出码**：单仓库模式 `0` 成功（含未启用跳过）、`1` 失败；全量模式汇总所有仓库，存在失败时返回 `1`。

## 10. 与 CI 方案的对比

| 维度 | 本方案（零 CI） | CI 方案（Woodpecker / Gitea Actions） |
| ---- | ---- | ---- |
| 持续性同步 | Gitea Push Mirror 原生（即时 + 定时兜底） | 同样依赖 Gitea Push Mirror 或镜像推送 |
| 初始化触发 | cron 扫描 / post-receive hook | CI 在 push 事件后运行 |
| 常驻组件 | 无（cron 一行） | CI server/agent/runner |
| 失败可见性 | Gitea 仓库“推送镜像”设置页 + 脚本日志 | CI 流水线日志 |
| 自研代码 | 单个幂等脚本 | 脚本 + workflow 模板 |
| 运维成本 | 最低 | 较高 |

结论：镜像的持续性同步不需要任何 CI；CI 只解决“初始化”这一幂等步骤，cron/hook 足以胜任，且组件更少、更成熟（全部使用官方能力）。


## 11. 测试场景

| 场景 | 预期结果 |
| ---- | -------- |
| 新 Gitea 仓库（GitHub 无同名仓库，`.github-sync.yml` enabled） | GitHub 自动建库（默认私有），Push Mirror 自动创建 |
| 仓库未添加 `.github-sync.yml` | 跳过，不产生任何 GitHub 调用 |
| `.github-sync.yml` 中 `enabled: false` | 同上，跳过 |
| GitHub 已有同名仓库 | 跳过创建，仅配置 Push Mirror |
| 已存在 Push Mirror | 跳过，不产生重复 |
| Mirror 被删除后再次扫描 / push | 检测缺失并自动重建 |
| 私有 / 公开仓库 | 由 `github.private` 控制创建时的 `private` 字段，默认私有 |
| cron 全量扫描 | 空/归档/镜像仓库跳过；已启用仓库幂等补齐 |
| post-receive hook | push 后即时完成该仓库初始化 |
| 已有镜像配置的仓库 | 每次运行均为幂等无副作用 |

## 12. 本地测试

无需真实凭据与网络，单元测试通过 mock HTTP 覆盖初始化逻辑：

```bash
python3 -m unittest discover -s tests -v
```

本地端到端验证“未启用同步”的跳过行为：

```bash
tmp=$(mktemp -d)
printf 'github:\n  enabled: false\n' > "$tmp/.github-sync.yml"
python3 sync/gitea_github_sync.py --repo-owner alice --repo-name repo \
  --config "$tmp/.github-sync.yml"
echo "exit=$?"
```

## 13. 安全注意事项

- **凭据本地化**：GitHub PAT 与 Gitea API Token 只保存在服务器本地 `~/.config/gitea-push-github/gitea-push-github.env`（权限 600）；脚本不打印、不落盘、不进仓库；
- `.gitignore` 已忽略 `*.env` / `.env`，防止误提交；
- 遵循最小权限：GitHub Token 仅授予本方案所需 scope，Gitea Token 仅 `write:repository`；
- post-receive hook 内容存于 Gitea 数据库（非仓库文件）；建议在其中使用绝对路径与日志重定向，避免泄露与乱码；
- 幂等且非破坏：脚本只创建缺失的仓库与 Mirror，不会删除或修改已存在的 GitHub 仓库可见性。

## 14. 参考资料

- [Gitea Mirror Repository（官方文档，含 push mirror 与 post-receive hook 说明）](https://docs.gitea.com/usage/repository/repo-mirror)
- [Gitea API 文档](https://docs.gitea.com/api/1.27)
- [GitHub REST API](https://docs.github.com/en/rest)
- [GitHub CLI（gh）](https://cli.github.com/manual/gh_repo_create)
- [Gitea 官方二进制安装](https://docs.gitea.com/zh-cn/installation/install-from-binary)

