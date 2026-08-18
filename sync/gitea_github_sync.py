#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gitea -> GitHub 自动镜像供给脚本（幂等，无需 CI）。

职责：
  1. 读取仓库级配置 .github-sync.yml；缺省或 github.enabled != true 时跳过
     （即“默认不同步”）。
  2. 检查 GitHub 仓库是否存在，不存在则以 private=true（默认）创建。
  3. 检查 Gitea Push Mirror 是否存在，不存在则创建。
  创建完成后，后续每次 push 由 Gitea Push Mirror（sync_on_commit）自动同步到 GitHub；
  本脚本只负责“初始化/兜底”，不需要任何 CI 组件。

两种运行方式：
  a) 全量扫描（cron / 手动）：
       gitea_github_sync.py --repo-owner <owner>
     遍历该用户全部仓库，逐个按 .github-sync.yml 判断并 ensure。
  b) 单仓库（Gitea post-receive hook 按需触发）：
       gitea_github_sync.py --repo-owner <owner> --repo-name <name>

仓库级配置 .github-sync.yml（缺省即不同步，见 examples/github-sync.yml）：
  github:
    enabled: true    # true 才同步；默认 false
    private: true    # GitHub 云端仓库可见性；默认 true（私有）

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

GitHub 建库使用 GitHub 官方 REST API（也可用官方 gh CLI 手动/辅助完成，
如 gh repo create <name> --private）。
"""

import argparse
import base64
import json
import os
import sys
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


def get_env(name, default=None):
    value = os.environ.get(name, "")
    return value if value else default


def log(message):
    print(f"[sync] {message}")


def fail(message, exit_code=1):
    print(f"[sync] 错误: {message}", file=sys.stderr)
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


def load_repo_config(path):
    """读取本地同步配置文件；文件不存在时返回 None（即默认不同步）。"""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return parse_repo_config_text(handle.read())


def config_enabled(config):
    if not isinstance(config, dict):
        return False
    section = config.get("github")
    if not isinstance(section, dict):
        return False
    return bool(section.get("enabled", False))


def config_private(config):
    if not isinstance(config, dict):
        return True
    section = config.get("github")
    if not isinstance(section, dict):
        return True
    return bool(section.get("private", True))


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


def github_repo_exists(owner, repo, token):
    code, _ = http_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}", github_headers(token))
    return code == 200


def github_create_repo(repo, private, token, description):
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


def gitea_get_file(api_url, owner, repo, path, ref, token):
    """读取仓库内文件文本；不存在或读取失败返回 None。"""
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


def gitea_list_push_mirrors(api_url, owner, repo, token):
    url = f"{api_url}/{GITEA_API_PREFIX}/repos/{owner}/{repo}/push_mirrors"
    return http_request("GET", url, gitea_headers(token))


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


def ensure_repo(owner, repo, creds, config_arg=None):
    """幂等确保单个仓库：GitHub 云端仓库 + Gitea Push Mirror。返回是否成功。"""
    github_username = creds.get("GITHUB_USERNAME")
    github_token = creds.get("GITHUB_TOKEN")
    gitea_api_url = (creds.get("GITEA_API_URL") or "").rstrip("/")
    gitea_token = creds.get("GITEA_TOKEN")

    # 读取仓库级配置：本地 --config 优先，否则通过 Gitea API 读取 .github-sync.yml
    if config_arg:
        config = load_repo_config(config_arg)
    else:
        text = gitea_get_file(gitea_api_url, owner, repo, REPO_CONFIG, "HEAD", gitea_token)
        config = parse_repo_config_text(text) if text else None
    if not config_enabled(config):
        log(f"{owner}/{repo}: GitHub 同步未启用（缺少 .github-sync.yml 或 enabled != true），跳过")
        return True
    private = config_private(config)
    log(f"{owner}/{repo}: 同步已启用，云端仓库创建时可见性 private={private}")

    remote_address = f"https://github.com/{github_username}/{repo}.git"
    description = f"Mirror of {owner}/{repo} (managed by gitea-push-github)"

    # ---- 步骤 1: GitHub 仓库检查 / 创建 ----
    if github_repo_exists(github_username, repo, github_token):
        log(f"GitHub 仓库 {github_username}/{repo} 已存在，跳过创建")
    else:
        log(f"GitHub 仓库 {github_username}/{repo} 不存在，开始创建 (private={private})")
        code, payload = github_create_repo(repo, private, github_token, description)
        if code in (200, 201):
            log("GitHub 仓库创建成功")
        else:
            reason = payload.get("message", payload) if isinstance(payload, dict) else payload
            log(f"GitHub 仓库创建失败 (HTTP {code}): {reason}")
            return False

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
        "--config",
        help="本地同步配置文件；缺省通过 Gitea API 读取仓库内 .github-sync.yml",
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
        return 0 if ensure_repo(repo_owner, args.repo_name, creds, args.config) else 1

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
        if ensure_repo(repo_owner, name, creds, args.config):
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

