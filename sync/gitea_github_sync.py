#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gitea -> GitHub 自动 Push Mirror 供给脚本（幂等）。

职责：
  1. 检查 GitHub 仓库是否存在，不存在则创建
  2. 检查 Gitea Push Mirror 是否存在，不存在则创建
创建完成后，后续 push 由 Gitea Push Mirror 自动同步到 GitHub。

用法：gitea_github_sync.py --repo-owner <owner> --repo-name <name>

环境变量（Woodpecker Secrets 注入）：
  GITHUB_USERNAME  GitHub 用户名（固定目标账号）
  GITHUB_TOKEN     GitHub Personal Access Token
  GITEA_API_URL    Gitea 实例基础地址，如 https://git.example.com
  GITEA_TOKEN      Gitea API Token（scope: write:repository）
  GITHUB_PRIVATE   可选，true/false，控制目标仓库可见性，默认 false
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"
GITEA_API_PREFIX = "api/v1"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"
PUSH_MIRROR_INTERVAL = "8h0m0s"  # Gitea 定时兜底同步间隔


def get_env(name, default=None):
    value = os.environ.get(name, "")
    return value if value else default


def log(message):
    print(f"[sync] {message}")


def fail(message, exit_code=1):
    print(f"[sync] 错误: {message}", file=sys.stderr)
    sys.exit(exit_code)


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


def main():
    parser = argparse.ArgumentParser(description="Gitea -> GitHub Push Mirror 自动供给")
    parser.add_argument("--repo-owner", help="Gitea 仓库所有者")
    parser.add_argument("--repo-name", help="Gitea 仓库名称")
    args = parser.parse_args()

    repo_owner = args.repo_owner or get_env("CI_REPO_OWNER")
    repo_name = args.repo_name or get_env("CI_REPO_NAME")

    github_username = get_env("GITHUB_USERNAME")
    github_token = get_env("GITHUB_TOKEN")
    gitea_api_url = get_env("GITEA_API_URL")
    gitea_token = get_env("GITEA_TOKEN")
    github_private = get_env("GITHUB_PRIVATE", "false").strip().lower() in (
        "true",
        "1",
        "yes",
    )

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
        log(
            f"GitHub 仓库 {github_username}/{repo_name} 不存在，"
            f"开始创建 (private={github_private})"
        )
        code, payload = github_create_repo(
            repo_name, github_private, github_token, description
        )
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
        if code != 200:
            reason = payload.get("message", payload) if isinstance(payload, dict) else payload
            fail(f"Push Mirror 创建失败 (HTTP {code}): {reason}")
        log("Push Mirror 创建成功")

    log("同步就绪：未来 push 将由 Gitea Push Mirror 自动转发到 GitHub")


if __name__ == "__main__":
    main()
