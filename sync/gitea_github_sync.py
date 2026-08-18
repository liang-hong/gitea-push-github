#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gitea -> GitHub 自动 Push Mirror 供给脚本（幂等，默认私有、默认不同步）。

职责：
  1. 读取仓库级配置 .github-sync.yml；缺省或 github.enabled != true 时直接跳过
     （即“默认不同步”）。
  2. 检查 GitHub 仓库是否存在，不存在则以 private=true（默认）创建。
  3. 检查 Gitea Push Mirror 是否存在，不存在则创建。
  创建完成后，后续 push 由 Gitea Push Mirror 自动同步到 GitHub。

仓库级配置 .github-sync.yml（缺省即不同步，见 examples/github-sync.yml）：
  github:
    enabled: true    # true 才同步；默认 false
    private: true    # GitHub 云端仓库可见性；默认 true（私有）

凭据来源（优先级从高到低，详见 README）：
  1. --credentials 指定的本地文件
  2. 环境变量 GITEA_PUSH_GITHUB_CREDENTIALS 指定的本地文件
  3. 默认本地文件 /run/secrets/gitea-push-github/gitea-push-github.env
     （由 .woodpecker.yml 中的卷挂载只读注入）
  4. 进程环境变量（兼容 Woodpecker Secrets / 本地调试）

本地凭据文件格式（KEY=VALUE，支持 # 注释，权限建议 600）：
  GITHUB_USERNAME=octocat
  GITHUB_TOKEN=github_pat_xxxx
  GITEA_API_URL=https://git.example.com
  GITEA_TOKEN=xxxx

用法：
  gitea_github_sync.py --repo-owner <owner> --repo-name <name>
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

try:
    import yaml  # 可选依赖；缺失时使用内置最小解析器
except ImportError:
    yaml = None

GITHUB_API = "https://api.github.com"
GITEA_API_PREFIX = "api/v1"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"
PUSH_MIRROR_INTERVAL = "8h0m0s"  # Gitea 定时兜底同步间隔
DEFAULT_CREDENTIALS = "/run/secrets/gitea-push-github/gitea-push-github.env"
REPO_CONFIG = ".github-sync.yml"


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
        if os.path.isfile(path):
            return load_dotenv(path)

    # 回退到进程环境（兼容 Woodpecker Secrets / 本地调试）
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


def load_repo_config(path):
    """读取仓库级同步配置；文件不存在时返回 None（即默认不同步）。"""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if yaml is not None:
        try:
            parsed = yaml.safe_load(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass  # 回退到内置最小解析器
    return _parse_yaml_min(text)


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gitea -> GitHub Push Mirror 自动供给")
    parser.add_argument("--repo-owner", help="Gitea 仓库所有者")
    parser.add_argument("--repo-name", help="Gitea 仓库名称")
    parser.add_argument("--credentials", help="本地凭据文件路径")
    parser.add_argument("--config", help="仓库级同步配置路径（默认 ./.github-sync.yml）")
    args = parser.parse_args(argv)

    repo_owner = args.repo_owner or get_env("CI_REPO_OWNER")
    repo_name = args.repo_name or get_env("CI_REPO_NAME")

    config = load_repo_config(args.config or REPO_CONFIG)
    if not config_enabled(config):
        log("GitHub 同步未启用（缺少 .github-sync.yml 或 github.enabled != true），跳过")
        return 0
    private = config_private(config)
    log(f"GitHub 同步已启用；云端仓库创建时可见性 private={private}")

    creds = resolve_credentials(args.credentials)
    github_username = creds.get("GITHUB_USERNAME")
    github_token = creds.get("GITHUB_TOKEN")
    gitea_api_url = creds.get("GITEA_API_URL")
    gitea_token = creds.get("GITEA_TOKEN")

    missing = []
    for name, value in (
        ("repo_owner", repo_owner),
        ("repo_name", repo_name),
        ("GITHUB_USERNAME", github_username),
        ("GITHUB_TOKEN", github_token),
        ("GITEA_API_URL", gitea_api_url),
        ("GITEA_TOKEN", gitea_token),
    ):
        if not value:
            missing.append(name)
    if missing:
        fail(f"缺少必要配置: {', '.join(missing)}")

    gitea_api_url = gitea_api_url.rstrip("/")
    remote_address = f"https://github.com/{github_username}/{repo_name}.git"
    description = f"Mirror of {repo_owner}/{repo_name} (managed by gitea-push-github)"

    log(f"目标: Gitea {repo_owner}/{repo_name} -> GitHub {github_username}/{repo_name}")

    # ---- 步骤 1: GitHub 仓库检查 / 创建 ----
    if github_repo_exists(github_username, repo_name, github_token):
        log(f"GitHub 仓库 {github_username}/{repo_name} 已存在，跳过创建")
    else:
        log(f"GitHub 仓库 {github_username}/{repo_name} 不存在，开始创建 (private={private})")
        code, payload = github_create_repo(repo_name, private, github_token, description)
        if code in (200, 201):
            log("GitHub 仓库创建成功")
        else:
            reason = payload.get("message", payload) if isinstance(payload, dict) else payload
            fail(f"GitHub 仓库创建失败 (HTTP {code}): {reason}")

    # ---- 步骤 2: Gitea Push Mirror 检查 / 创建 ----
    code, payload = gitea_list_push_mirrors(gitea_api_url, repo_owner, repo_name, gitea_token)
    if code != 200:
        reason = payload.get("message", payload) if isinstance(payload, dict) else payload
        fail(f"Gitea Push Mirror 列表获取失败 (HTTP {code}): {reason}")

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
            gitea_api_url,
            repo_owner,
            repo_name,
            gitea_token,
            remote_address,
            github_username,
            github_token,
        )
        if code not in (200, 201):
            reason = payload.get("message", payload) if isinstance(payload, dict) else payload
            fail(f"Push Mirror 创建失败 (HTTP {code}): {reason}")
        log("Push Mirror 创建成功")

    log("同步就绪：未来 push 将由 Gitea Push Mirror 自动转发到 GitHub")
    return 0


if __name__ == "__main__":
    main()

