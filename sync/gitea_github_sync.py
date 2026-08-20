#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gitea -> GitHub 自动镜像供给脚本（幂等，无需 CI）。

职责：
  1. 读取仓库主目录 .github-sync.yml（固定位置/文件名），按 github.state 收敛到目标状态：
     enable   - GitHub 无同名仓库则创建（private 默认 true）；Push Mirror 缺失则创建（保留已存在者）
     suspend  - 删除指向本方案 GitHub 仓库的 Gitea Push Mirror；GitHub 仓库保留不删
     remove/disable（缺省） - 不创建也不删除任何内容
     多分支规则：仅当全部分支都有该文件且内容一致（忽略注释）时才执行策略；
     任一分支缺失或不一致 → 按 disable 处理（不报错）。state 值非法时报错。
  2. state=enable 时检查 GitHub 仓库是否存在：不存在则以 private=true（默认）创建（description 与 Gitea 一致）；
     已存在则按配置收敛可见性（PATCH）、默认分支与 description。
  3. state=enable 时检查 Gitea Push Mirror 是否存在，不存在则创建。
  创建完成后，后续每次 push 由 Gitea Push Mirror（sync_on_commit）自动同步到 GitHub；
  本脚本只负责“初始化/兜底”，不需要任何 CI 组件。

两种运行方式：
  a) 全量扫描（cron / 手动）：
       gitea_github_sync.py --repo-owner <owner>
     遍历该用户全部仓库，逐个按 .github-sync.yml 判断并 ensure。
  b) 单仓库（Gitea post-receive hook 按需触发）：
       gitea_github_sync.py --repo-owner <owner> --repo-name <name>

  --dry-run 只打印将执行的动作，不发起任何创建/删除（推荐先预览再执行）。

仓库级配置：固定为仓库主目录 .github-sync.yml（不使用其他位置/文件名，见 examples/github-sync.yml）：
  github:
    state: enable    # enable=创建 GitHub 仓库并保留/补齐 Push Mirror
                     # suspend=删除 Gitea Push Mirror（保留 GitHub 仓库，停止更新）
                     # remove/disable=不创建不删除（默认，二者等同）
    private: true    # GitHub 云端仓库可见性；默认 true（私有）
    default_branch: no-ci  # 可选；同时设置 Gitea 与 GitHub 云端仓库默认分支（未配置则不改动）

多分支：仅当全部分支都有 .github-sync.yml 且内容一致（忽略注释）时才执行策略；
任一分支缺失或不一致 → 按 disable 处理（不报错）。

凭据来源（优先级从高到低，详见 README）：
  1. --credentials 指定的本地文件
  2. 环境变量 GITEA_PUSH_GITHUB_CREDENTIALS 指定的本地文件
  3. 默认本地文件 ~/.config/gitea-push-github/gitea-push-github.env
  4. 进程环境变量（兼容本地调试）

本地凭据文件格式（KEY=VALUE，支持 # 注释，权限建议 600）：
  GITHUB_USERNAME=octocat
  GITHUB_TOKEN=github_pat_xxxx
  GITEA_API_URL=https://git.example.com
  GITEA_TOKEN=xxxx

可选 SMTP（token 过期时邮件提醒；未配置则只记日志）：
  SMTP_HOST=smtp.qq.com
  SMTP_PORT=465
  SMTP_USER=octocat@example.com
  SMTP_PASSWORD=授权码
  SMTP_FROM=octocat@example.com   # 缺省同 SMTP_USER
  MAIL_TO=octocat@example.com     # 缺省同 SMTP_USER

GitHub 建库使用 GitHub 官方 REST API（也可用官方 gh CLI 手动/辅助完成，
如 gh repo create <name> --private）。
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

try:
    import yaml  # 可选依赖；缺失时使用内置最小解析器
except ImportError:
    yaml = None

GITHUB_API = "https://api.github.com"
GITEA_API_PREFIX = "api/v1"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"
PUSH_MIRROR_INTERVAL = "8h0m0s"  # Gitea 定时兜底同步间隔
DEFAULT_CREDENTIALS = "~/.config/gitea-push-github/gitea-push-github.env"
REPO_CONFIG = ".github-sync.yml"
PAGE_SIZE = 50

# github.state 合法值；disable 等同 remove（缺省），详见 config_state()
LEGAL_STATES = ("enable", "suspend", "remove", "disable")
DEFAULT_STATE = "remove"

# git 常见默认主分支名；分支参数为空时回退（优先 main，其次 master）
DEFAULT_BRANCH_CANDIDATES = ("main", "master")


def get_env(name, default=None):
    value = os.environ.get(name, "")
    return value if value else default


def log(message):
    # 系统时间 + 本地时区名/偏移（如 2026-08-21 01:23:09 CST (+0800)）
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
    print(f"[sync] {stamp} {message}")


def fail(message, exit_code=1):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
    print(f"[sync] {stamp} 错误: {message}", file=sys.stderr)
    sys.exit(exit_code)


def load_dotenv(path):
    """解析 KEY=VALUE 本地凭据文件，忽略空行与 # 注释。"""
    values = {}
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def resolve_credentials(credentials_arg=None):
    """按优先级解析凭据：显式文件 -> 环境变量指定文件 -> 默认文件 -> 进程环境。"""
    candidates = []
    if credentials_arg:
        candidates.append(credentials_arg)
    env_path = get_env("GITEA_PUSH_GITHUB_CREDENTIALS")
    if env_path:
        candidates.append(env_path)
    candidates.append(DEFAULT_CREDENTIALS)

    for path in candidates:
        path = os.path.expanduser(path)
        if os.path.isfile(path):
            return load_dotenv(path)

    # 回退到进程环境（兼容本地调试 / 旧 Woodpecker Secrets 方式）
    return {
        "GITHUB_USERNAME": get_env("GITHUB_USERNAME"),
        "GITHUB_TOKEN": get_env("GITHUB_TOKEN"),
        "GITEA_API_URL": get_env("GITEA_API_URL"),
        "GITEA_TOKEN": get_env("GITEA_TOKEN"),
    }

# ---- 仓库级配置 .github-sync.yml 解析 ----


def _strip_comment(line):
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return ""
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"' and (i == 0 or line[i - 1] != "\\"):
            in_quote = not in_quote
        if ch == "#" and not in_quote and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _parse_scalar(value):
    value = value.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered in ("true", "yes", "on", "1"):
        return True
    if lowered in ("false", "no", "off", "0"):
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_yaml_min(text):
    """内置最小 YAML 解析器：仅支持顶层键与二级缩进标量（受控格式）。"""
    root = {}
    section = None
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            key, _, rest = line.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest:
                root[key] = _parse_scalar(rest)
                section = None
            else:
                root[key] = {}
                section = key
        elif indent > 0 and section and isinstance(root.get(section), dict):
            key, _, rest = line.strip().partition(":")
            root[section][key.strip()] = _parse_scalar(rest.strip())
    return root


def parse_repo_config_text(text):
    """从文本解析仓库级同步配置（优先 PyYAML，缺失时用内置最小解析器）。"""
    if yaml is not None:
        try:
            parsed = yaml.safe_load(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass  # 回退到内置最小解析器
    return _parse_yaml_min(text)


def config_state(config):
    """读取 github.state 并归一化。返回 "enable" / "suspend" / "remove"。

    缺配置、无 github 段或未写 state → 默认 "remove"（disable 等同 remove）。
    state 值非法时抛 ValueError，由调用方转为错误退出。
    """
    if not isinstance(config, dict):
        return DEFAULT_STATE
    section = config.get("github")
    if not isinstance(section, dict):
        return DEFAULT_STATE
    state = section.get("state")
    if state is None:
        return DEFAULT_STATE
    state = str(state).strip().lower()
    if state not in LEGAL_STATES:
        raise ValueError(
            f"github.state 非法: {state!r}（合法值: enable / suspend / remove / disable）"
        )
    if state == "disable":
        state = DEFAULT_STATE
    return state


def config_private(config):
    if not isinstance(config, dict):
        return True
    section = config.get("github")
    if not isinstance(section, dict):
        return True
    return bool(section.get("private", True))


def config_default_branch(config):
    """读取 github.default_branch；未配置时返回 None（不改动默认分支）。"""
    if not isinstance(config, dict):
        return None
    section = config.get("github")
    if not isinstance(section, dict):
        return None
    value = section.get("default_branch")
    if value is None:
        return None
    return str(value).strip() or None


def branch_or_default(branch, exists=None):
    """分支参数为空时回退到 git 常见默认主分支名（优先 main，其次 master）。

    exists 为可调用对象（入参为分支名）时，会尝试探测存在的主分支；未提供 exists
    或候选都不存在时返回 "main"。
    """
    if branch:
        return branch
    for candidate in DEFAULT_BRANCH_CANDIDATES:
        if exists is None or exists(candidate):
            return candidate
    return DEFAULT_BRANCH_CANDIDATES[0]


# ---- HTTP 与 API ----


def github_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": GITHUB_ACCEPT_HEADER,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "Content-Type": "application/json",
    }


def gitea_headers(token):
    return {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }


def http_request(method, url, headers, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = None
            return response.status, payload
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return error.code, payload


def github_token_valid(token):
    """校验 GitHub token 是否有效（无效/过期返回 False）。GET /rate_limit 无需 scope。"""
    code, _ = http_request("GET", f"{GITHUB_API}/rate_limit", github_headers(token))
    return code == 200


def send_expiry_email(creds, owner, repo):
    """SMTP 邮件提醒 GitHub token 过期（凭据文件未配 SMTP 时仅记日志，不阻塞）。"""
    host = creds.get("SMTP_HOST")
    if not host:
        log("未配置 SMTP_HOST，跳过邮件提醒（可在凭据文件添加 SMTP_* 配置）")
        return
    try:
        import smtplib
        from email.mime.text import MIMEText

        port = int(creds.get("SMTP_PORT") or 465)
        user = creds.get("SMTP_USER") or ""
        password = creds.get("SMTP_PASSWORD") or ""
        sender = creds.get("SMTP_FROM") or user
        to = creds.get("MAIL_TO") or user
        body = (
            f"gitea-push-github 检测到 GitHub Token 无效或已过期：{owner}/{repo}\n\n"
            "已删除该仓库的 Gitea Push Mirror（云端 GitHub 仓库数据未改动）。\n"
            "请在 GitHub 网页重新生成 fine-grained token，并更新凭据文件：\n"
            "  ~/.config/gitea-push-github/gitea-push-github.env\n"
            "更新后下次 cron 运行将自动重建 Push Mirror。\n"
        )
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[gitea-push-github] GitHub Token 已过期: {owner}/{repo}"
        msg["From"] = sender
        msg["To"] = to
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        try:
            if user:
                server.login(user, password)
            server.sendmail(sender, [to], msg.as_string())
        finally:
            server.quit()
        log(f"邮件提醒已发送至 {to}")
    except Exception as exc:
        log(f"邮件提醒发送失败: {exc}")


def github_get_repo(owner, repo, token):
    """获取 GitHub 仓库信息（含可见性 private 字段）。"""
    return http_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}", github_headers(token))


def github_branch_exists(owner, repo, branch, token):
    """检查 GitHub 仓库某分支是否存在（PATCH 默认分支前先确认）。"""
    branch = branch_or_default(branch)  # 空值回退 main
    url = f"{GITHUB_API}/repos/{owner}/{repo}/branches/{quote(str(branch), safe='')}"
    code, _ = http_request("GET", url, github_headers(token))
    return code == 200


def github_update_repo(owner, repo, fields, token):
    """PATCH 修改已存在 GitHub 仓库（可见性 / 默认分支等字段）。"""
    return http_request("PATCH", f"{GITHUB_API}/repos/{owner}/{repo}", github_headers(token), fields)


def github_create_repo(repo, private, token, description):
    """创建 GitHub 仓库；description 与 Gitea 仓库一致。"""
    body = {
        "name": repo,
        "private": private,
        "description": description,
        "auto_init": False,
        "has_issues": True,
        "has_wiki": True,
    }
    code, payload = http_request("POST", f"{GITHUB_API}/user/repos", github_headers(token), body)
    return code, payload


def gitea_list_repos(api_url, owner, token):
    """分页获取指定用户在 Gitea 上的全部仓库。"""
    repos = []
    page = 1
    while True:
        url = (
            f"{api_url}/{GITEA_API_PREFIX}/users/{quote(owner)}/repos"
            f"?page={page}&limit={PAGE_SIZE}"
        )
        code, payload = http_request("GET", url, gitea_headers(token))
        if code != 200:
            reason = payload.get("message", payload) if isinstance(payload, dict) else payload
            fail(f"仓库列表获取失败 (HTTP {code}): {reason}")
        if not isinstance(payload, list) or not payload:
            break
        repos.extend(payload)
        if len(payload) < PAGE_SIZE:
            break
        page += 1
    return repos


def gitea_list_branches(api_url, owner, repo, token):
    """分页获取仓库全部分支名；失败返回 None。"""
    names = []
    page = 1
    while True:
        url = (
            f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}/branches"
            f"?page={page}&limit={PAGE_SIZE}"
        )
        code, payload = http_request("GET", url, gitea_headers(token))
        if code != 200:
            reason = payload.get("message", payload) if isinstance(payload, dict) else payload
            log(f"分支列表获取失败 (HTTP {code}): {reason}")
            return None
        if not isinstance(payload, list) or not payload:
            break
        names.extend(
            item.get("name") for item in payload
            if isinstance(item, dict) and item.get("name")
        )
        if len(payload) < PAGE_SIZE:
            break
        page += 1
    return names


def load_repo_config_via_api(gitea_api_url, owner, repo, token):
    """按规则读取仓库统一配置（固定仓库主目录 .github-sync.yml）。

    多分支规则：仅当全部分支都有 .github-sync.yml 且内容一致（忽略注释，按解析结果
    比较）时返回解析后的配置；任一分支缺失或不一致 → 返回 None（按 disable 处理，
    不报错）。分支列表获取失败同样按 disable 处理（安全方向）。
    """
    branches = gitea_list_branches(gitea_api_url, owner, repo, token)
    if not branches:
        return None
    texts = []
    for branch in branches:
        text = gitea_get_file(gitea_api_url, owner, repo, REPO_CONFIG, branch, token)
        if text is None:
            log(f"{owner}/{repo}: 分支 {branch} 缺少 {REPO_CONFIG}，按 disable 处理")
            return None
        texts.append(text)
    parsed = [parse_repo_config_text(text) for text in texts]
    first = parsed[0]
    for index, item in enumerate(parsed[1:], start=1):
        if item != first:
            log(f"{owner}/{repo}: 分支间 {REPO_CONFIG} 内容不一致（忽略注释），按 disable 处理")
            return None
    return first


def gitea_get_file(api_url, owner, repo, path, ref, token):
    """读取仓库内文件文本；不存在或读取失败返回 None。"""
    ref = branch_or_default(ref)  # 分支参数为空时回退 main（git 默认主分支）
    url = (
        f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}"
        f"/contents/{quote(path)}?ref={quote(ref)}"
    )
    code, payload = http_request("GET", url, gitea_headers(token))
    if code == 404:
        return None
    if code != 200 or not isinstance(payload, dict):
        log(f"仓库 {owner}/{repo} 文件 {path} 读取失败 (HTTP {code})，视为未启用")
        return None
    content = payload.get("content", "")
    try:
        return base64.b64decode(content).decode("utf-8")
    except Exception:
        return None


def gitea_get_repo(api_url, owner, repo, token):
    """获取 Gitea 仓库信息（含 default_branch 字段）。"""
    url = f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}"
    return http_request("GET", url, gitea_headers(token))


def gitea_branch_exists(api_url, owner, repo, branch, token):
    """检查 Gitea 仓库某分支是否存在。"""
    branch = branch_or_default(branch)  # 空值回退 main
    url = (
        f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}"
        f"/branches/{quote(str(branch), safe='')}"
    )
    code, _ = http_request("GET", url, gitea_headers(token))
    return code == 200


def gitea_update_repo_default_branch(api_url, owner, repo, branch, token):
    """PATCH 修改 Gitea 仓库默认分支。"""
    url = f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}"
    return http_request("PATCH", url, gitea_headers(token), {"default_branch": branch})


def gitea_list_push_mirrors(api_url, owner, repo, token):
    url = f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}/push_mirrors"
    return http_request("GET", url, gitea_headers(token))


def gitea_delete_push_mirror(api_url, owner, repo, name, token):
    """按 remote_name 删除 Push Mirror；204/404 均视为删除成功（幂等）。"""
    url = (
        f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}"
        f"/push_mirrors/{quote(str(name), safe='')}"
    )
    return http_request("DELETE", url, gitea_headers(token))


def gitea_add_push_mirror(api_url, owner, repo, token, remote_address, username, password):
    url = f"{api_url}/{GITEA_API_PREFIX}/repos/{owner}/{repo}/push_mirrors"
    body = {
        "remote_address": remote_address,
        "remote_username": username,
        "remote_password": password,
        "interval": PUSH_MIRROR_INTERVAL,
        "sync_on_commit": True,
    }
    return http_request("POST", url, gitea_headers(token), body)


def suspend_push_mirror(api_url, owner, repo, token, remote_address, dry_run=False):
    """state=suspend：删除指向 remote_address 的 Gitea Push Mirror，保留 GitHub 仓库。"""
    code, payload = gitea_list_push_mirrors(api_url, owner, repo, token)
    if code != 200:
        reason = payload.get("message", payload) if isinstance(payload, dict) else payload
        log(f"Push Mirror 列表获取失败 (HTTP {code}): {reason}")
        return False

    mirrors = payload if isinstance(payload, list) else []
    matches = [
        m
        for m in mirrors
        if isinstance(m, dict) and m.get("remote_address") == remote_address
    ]
    if not matches:
        log(f"{owner}/{repo}: state=suspend，未找到匹配 Push Mirror ({remote_address})，无需删除")
        return True

    ok = True
    for mirror in matches:
        name = mirror.get("remote_name")
        if not name:
            log(f"{owner}/{repo}: Push Mirror 缺少 remote_name，无法删除，跳过")
            ok = False
            continue
        if dry_run:
            log(f"dry-run: 将删除 Push Mirror ({name}, {remote_address})")
            continue
        code, payload = gitea_delete_push_mirror(api_url, owner, repo, name, token)
        if code in (204, 404):
            log(f"Push Mirror 已删除 ({name}, {remote_address})")
        else:
            reason = payload.get("message", payload) if isinstance(payload, dict) else payload
            log(f"Push Mirror 删除失败 (HTTP {code}): {reason}")
            ok = False
    return ok


def ensure_repo(owner, repo, creds, dry_run=False):
    """按仓库主目录 .github-sync.yml 的 github.state 幂等收敛到目标状态。返回是否成功。

    配置固定为仓库主目录 .github-sync.yml；多分支仓库仅当全部分支都有该文件且内容
    一致（忽略注释）时执行策略，否则按 disable 处理（不报错）。

    state:
      enable  - GitHub 仓库缺失则创建（description 与 Gitea 一致）、已存在则按配置收敛可见性/默认分支/description；Gitea 默认分支也按配置收敛；Push Mirror 缺失则创建（保留已存在者）
      suspend - 删除指向本方案 GitHub 的 Push Mirror（保留 GitHub 仓库，停止更新）
      remove/disable（缺省） - 不创建也不删除任何内容（无操作）
    非法 state 值报错并返回 False。dry_run=True 时只打印将执行的动作，不发起写操作。
    """
    github_username = creds.get("GITHUB_USERNAME")
    github_token = creds.get("GITHUB_TOKEN")
    gitea_api_url = (creds.get("GITEA_API_URL") or "").rstrip("/")
    gitea_token = creds.get("GITEA_TOKEN")

    # 读取仓库级配置：固定经 Gitea API 读取仓库主目录 .github-sync.yml（多分支一致性校验）
    config = load_repo_config_via_api(gitea_api_url, owner, repo, gitea_token)

    try:
        state = config_state(config)
    except ValueError as exc:
        log(f"{owner}/{repo}: {exc}")
        return False

    remote_address = f"https://github.com/{github_username}/{repo}.git"

    if state == "remove":
        log(f"{owner}/{repo}: state=remove（默认），不创建/删除任何内容，跳过")
        return True

    if state == "suspend":
        return suspend_push_mirror(gitea_api_url, owner, repo, gitea_token, remote_address, dry_run)

    # ---- state == "enable" ----
    # 先校验 GitHub token：过期则删除 Push Mirror 并邮件提醒，不动云端 GitHub 数据
    if not github_token_valid(github_token):
        log("GitHub Token 无效或已过期（HTTP 401），删除 Push Mirror 暂停同步")
        suspend_push_mirror(gitea_api_url, owner, repo, gitea_token, remote_address, dry_run)
        if dry_run:
            log("dry-run: 将发送 token 过期邮件提醒")
        else:
            send_expiry_email(creds, owner, repo)
        log("请更新凭据文件后重试（下次运行自动重建 Push Mirror）")
        return False

    private = config_private(config)
    default_branch = config_default_branch(config)
    log(
        f"{owner}/{repo}: state=enable，目标可见性 private={private}，"
        f"默认分支 default_branch={default_branch or '(不改动)'}"
    )
    # ---- 步骤 0: 读取 Gitea 仓库信息（description 与当前默认分支） ----
    code, gitea_payload = gitea_get_repo(gitea_api_url, owner, repo, gitea_token)
    if code != 200 or not isinstance(gitea_payload, dict):
        reason = gitea_payload.get("message", gitea_payload) if isinstance(gitea_payload, dict) else gitea_payload
        log(f"Gitea 仓库信息获取失败 (HTTP {code}): {reason}")
        return False
    gitea_description = gitea_payload.get("description") or ""
    log(f"Gitea 仓库 {owner}/{repo} description={gitea_description!r}")

    # ---- 步骤 1: GitHub 仓库检查 / 创建 ----
    repo_payload = None
    code, payload = github_get_repo(github_username, repo, github_token)
    if code == 200 and isinstance(payload, dict):
        repo_payload = payload
        log(f"GitHub 仓库 {github_username}/{repo} 已存在")
    elif code == 404:
        if dry_run:
            log(f"dry-run: 将创建 GitHub 仓库 {github_username}/{repo} (private={private}, description={gitea_description!r})")
        else:
            log(f"GitHub 仓库 {github_username}/{repo} 不存在，开始创建 (private={private}, description={gitea_description!r})")
            code, payload = github_create_repo(repo, private, github_token, gitea_description)
            if code in (200, 201) and isinstance(payload, dict):
                repo_payload = payload
                log("GitHub 仓库创建成功")
            else:
                reason = payload.get("message", payload) if isinstance(payload, dict) else payload
                log(f"GitHub 仓库创建失败 (HTTP {code}): {reason}")
                return False
    else:
        reason = payload.get("message", payload) if isinstance(payload, dict) else payload
        log(f"GitHub 仓库检查失败 (HTTP {code}): {reason}")
        return False

    # ---- 步骤 1b: 收敛可见性 / 默认分支 / description（合并为单次 PATCH） ----
    if repo_payload is None:
        log("dry-run: 仓库创建后将按配置收敛可见性/默认分支/描述")
    else:
        updates = {}
        current_private = repo_payload.get("private")
        if current_private is not None and bool(current_private) != private:
            if not private:
                log(f"警告: GitHub 仓库 {github_username}/{repo} 将由私有改为公开（private=false）")
            updates["private"] = private
        if default_branch:
            current_default = branch_or_default(
                repo_payload.get("default_branch"),
                lambda branch: github_branch_exists(github_username, repo, branch, github_token),
            )
            if default_branch != current_default:
                if github_branch_exists(github_username, repo, default_branch, github_token):
                    updates["default_branch"] = default_branch
                else:
                    log(f"警告: 分支 {default_branch} 尚未同步到 GitHub 仓库，暂不设置默认分支（镜像同步后下次运行生效）")
        current_description = repo_payload.get("description") or ""
        if current_description != gitea_description:
            updates["description"] = gitea_description
        if updates:
            if dry_run:
                log(f"dry-run: 将 PATCH 更新 GitHub 仓库 {github_username}/{repo} {updates}")
            else:
                code2, payload2 = github_update_repo(github_username, repo, updates, github_token)
                if code2 in (200, 204):
                    log(f"GitHub 仓库已更新: {updates}")
                else:
                    reason = payload2.get("message", payload2) if isinstance(payload2, dict) else payload2
                    log(f"GitHub 仓库更新失败 (HTTP {code2}): {reason}")
                    return False
        else:
            log(f"GitHub 仓库 {github_username}/{repo} 可见性/默认分支/描述均与配置一致，跳过")

    # ---- 步骤 1c: 收敛 Gitea 默认分支（配置了 default_branch 时） ----
    if default_branch:
        current_gitea_default = branch_or_default(
            gitea_payload.get("default_branch"),
            lambda branch: gitea_branch_exists(gitea_api_url, owner, repo, branch, gitea_token),
        )
        if current_gitea_default != default_branch:
            if dry_run:
                log(f"dry-run: 将 PATCH Gitea 仓库默认分支 {owner}/{repo} -> {default_branch}")
            else:
                code2, payload2 = gitea_update_repo_default_branch(
                    gitea_api_url, owner, repo, default_branch, gitea_token
                )
                if code2 in (200, 204):
                    log(f"Gitea 仓库默认分支已更新: {owner}/{repo} -> {default_branch}")
                else:
                    reason = payload2.get("message", payload2) if isinstance(payload2, dict) else payload2
                    log(f"Gitea 仓库默认分支更新失败 (HTTP {code2}): {reason}")
                    return False
        else:
            log(f"Gitea 仓库 {owner}/{repo} 默认分支已是 {default_branch}")

    # ---- 步骤 2: Gitea Push Mirror 检查 / 创建 ----
    code, payload = gitea_list_push_mirrors(gitea_api_url, owner, repo, gitea_token)
    if code != 200:
        reason = payload.get("message", payload) if isinstance(payload, dict) else payload
        log(f"Push Mirror 列表获取失败 (HTTP {code}): {reason}")
        return False

    mirrors = payload if isinstance(payload, list) else []
    existing = [
        m
        for m in mirrors
        if isinstance(m, dict) and m.get("remote_address") == remote_address
    ]
    if existing:
        log(f"Push Mirror 已存在 ({remote_address})，跳过创建")
    elif dry_run:
        log(f"dry-run: 将创建 Push Mirror ({remote_address})")
    else:
        log(f"Push Mirror 不存在，开始创建 ({remote_address})")
        code, payload = gitea_add_push_mirror(
            gitea_api_url, owner, repo, gitea_token, remote_address, github_username, github_token
        )
        if code not in (200, 201):
            reason = payload.get("message", payload) if isinstance(payload, dict) else payload
            log(f"Push Mirror 创建失败 (HTTP {code}): {reason}")
            return False
        log("Push Mirror 创建成功")

    log(f"{owner}/{repo}: 同步就绪（后续 push 由 Gitea Push Mirror 自动转发）")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Gitea -> GitHub 自动镜像供给（幂等，无需 CI）"
    )
    parser.add_argument(
        "--repo-owner",
        help="Gitea 仓库所有者（缺省取 GITEA_REPO_USER_NAME / CI_REPO_OWNER）",
    )
    parser.add_argument(
        "--repo-name",
        help="Gitea 仓库名称；缺省时遍历所有者全部仓库",
    )
    parser.add_argument("--credentials", help="本地凭据文件路径")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的动作，不发起任何创建/删除",
    )
    args = parser.parse_args(argv)

    repo_owner = args.repo_owner or get_env("GITEA_REPO_USER_NAME") or get_env("CI_REPO_OWNER")
    if not repo_owner:
        fail("缺少 --repo-owner（或环境变量 GITEA_REPO_USER_NAME / CI_REPO_OWNER）")

    creds = resolve_credentials(args.credentials)
    missing = []
    for name, value in (
        ("GITHUB_USERNAME", creds.get("GITHUB_USERNAME")),
        ("GITHUB_TOKEN", creds.get("GITHUB_TOKEN")),
        ("GITEA_API_URL", creds.get("GITEA_API_URL")),
        ("GITEA_TOKEN", creds.get("GITEA_TOKEN")),
    ):
        if not value:
            missing.append(name)
    if missing:
        fail(f"缺少必要配置: {', '.join(missing)}")

    # ---- 单仓库模式（post-receive hook 等按需触发） ----
    if args.repo_name:
        return 0 if ensure_repo(repo_owner, args.repo_name, creds, args.dry_run) else 1

    # ---- 全量模式（cron / 手动），遍历所有者全部仓库 ----
    gitea_api_url = (creds.get("GITEA_API_URL") or "").rstrip("/")
    repos = gitea_list_repos(gitea_api_url, repo_owner, creds.get("GITEA_TOKEN"))
    log(f"共发现 {len(repos)} 个仓库（{repo_owner}）")

    updated, skipped, errors = [], [], []
    for repo in repos:
        name = repo.get("name", "")
        if repo.get("mirror"):
            log(f"{repo_owner}/{name}: 镜像仓库，跳过")
            skipped.append(name)
            continue
        if repo.get("archived"):
            log(f"{repo_owner}/{name}: 已归档，跳过")
            skipped.append(name)
            continue
        if repo.get("empty"):
            log(f"{repo_owner}/{name}: 空仓库（无默认分支），跳过")
            skipped.append(name)
            continue
        if ensure_repo(repo_owner, name, creds, args.dry_run):
            updated.append(name)
        else:
            errors.append(name)

    log("=" * 60)
    log(f"完成：就绪 {len(updated)}，跳过 {len(skipped)}，失败 {len(errors)}")
    if errors:
        log(f"失败仓库: {', '.join(errors)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

