#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给缺少 .woodpecker.yml 的 Gitea 仓库自动注入同步流水线模板（幂等）。

默认行为：
  - 注入的模板默认【不同步】到 GitHub（需仓库根目录 .github-sync.yml 显式启用，
    见 examples/github-sync.yml）。
  - 不触碰已存在 .woodpecker.yml 的仓库。
  - 跳过空仓库（无默认分支，无法通过 API 创建文件）、归档仓库、镜像仓库。

凭据：从本地文件读取 Gitea API 地址与 Token（默认
~/.config/gitea-push-github/gitea-push-github.env），绝不写入仓库。

模板占位符（templates/woodpecker.yml）：
  {{SYNC_IMAGE}}    同步工具镜像，取自本地凭据文件 SYNC_IMAGE 或 --sync-image
  {{SECRETS_MOUNT}} 宿主机凭据目录（只读挂载到流水线容器），取自本地凭据文件
                    SECRETS_MOUNT 或 --secrets-mount；缺省为凭据文件所在目录

用法：
  python3 provision/add_woodpecker_yml.py --owner <owner> --dry-run
  python3 provision/add_woodpecker_yml.py --owner <owner> --exclude gitea-push-github
  python3 provision/add_woodpecker_yml.py --owner <owner> --repo repo-a --repo repo-b
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

GITEA_API_PREFIX = "api/v1"
DEFAULT_CREDENTIALS = "~/.config/gitea-push-github/gitea-push-github.env"
WOODPECKER_FILE = ".woodpecker.yml"
PAGE_SIZE = 50
DEFAULT_COMMIT_MESSAGE = (
    "ci: add woodpecker sync template\n"
    "\n"
    "Add repo-level sync pipeline template with GitHub sync disabled by default.\n"
    "中文：新增仓库级同步流水线模板，默认不启用 GitHub 同步。"
)


def log(message):
    print(f"[provision] {message}")


def fail(message, exit_code=1):
    print(f"[provision] 错误: {message}", file=sys.stderr)
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


def gitea_file_exists(api_url, owner, repo, path, ref, token):
    url = (
        f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}"
        f"/contents/{quote(path)}?ref={quote(ref)}"
    )
    code, _ = http_request("GET", url, gitea_headers(token))
    if code == 200:
        return True
    if code == 404:
        return False
    fail(f"仓库 {owner}/{repo} 文件 {path} 检查失败 (HTTP {code})")
    return False  # 不可达

def gitea_create_file(api_url, owner, repo, path, content, message, branch, token):
    url = (
        f"{api_url}/{GITEA_API_PREFIX}/repos/{quote(owner)}/{quote(repo)}"
        f"/contents/{quote(path)}"
    )
    body = {
        "branch": branch,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "message": message,
    }
    return http_request("POST", url, gitea_headers(token), body)


def render_template(text, sync_image, secrets_mount):
    if not sync_image:
        fail("缺少同步工具镜像（本地凭据文件 SYNC_IMAGE 或 --sync-image）")
    if not secrets_mount:
        fail("缺少宿主机凭据目录（本地凭据文件 SECRETS_MOUNT 或 --secrets-mount）")
    return text.replace("{{SYNC_IMAGE}}", sync_image).replace(
        "{{SECRETS_MOUNT}}", secrets_mount
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="向缺少 .woodpecker.yml 的 Gitea 仓库注入同步流水线模板"
    )
    parser.add_argument("--owner", required=True, help="Gitea 仓库所有者（用户名）")
    parser.add_argument(
        "--credentials",
        default=DEFAULT_CREDENTIALS,
        help="本地凭据文件路径（默认 ~/.config/gitea-push-github/gitea-push-github.env）",
    )
    parser.add_argument(
        "--template",
        default=str(Path(__file__).resolve().parent.parent / "templates" / "woodpecker.yml"),
        help="流水线模板路径",
    )
    parser.add_argument("--repo", action="append", default=[], help="仅处理指定仓库（可多次）")
    parser.add_argument("--exclude", action="append", default=[], help="排除指定仓库（可多次）")
    parser.add_argument("--sync-image", help="同步工具镜像（覆盖模板 {{SYNC_IMAGE}}）")
    parser.add_argument("--secrets-mount", help="宿主机凭据目录（覆盖模板 {{SECRETS_MOUNT}}）")
    parser.add_argument("--commit-message", default=DEFAULT_COMMIT_MESSAGE, help="注入文件的提交信息")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不创建")
    args = parser.parse_args(argv)

    credentials_path = os.path.expanduser(args.credentials)
    if not os.path.isfile(credentials_path):
        fail(f"本地凭据文件不存在: {credentials_path}")
    creds = load_dotenv(credentials_path)

    gitea_api_url = (creds.get("GITEA_API_URL") or "").rstrip("/")
    gitea_token = creds.get("GITEA_TOKEN")
    missing = []
    if not gitea_api_url:
        missing.append("GITEA_API_URL")
    if not gitea_token:
        missing.append("GITEA_TOKEN")
    if missing:
        fail(f"本地凭据文件缺少必要配置: {', '.join(missing)}")

    sync_image = args.sync_image or creds.get("SYNC_IMAGE")
    secrets_mount = (
        args.secrets_mount or creds.get("SECRETS_MOUNT") or str(Path(credentials_path).parent)
    )

    template_path = Path(args.template)
    if not template_path.is_file():
        fail(f"模板文件不存在: {template_path}")
    template_text = template_path.read_text(encoding="utf-8")

    only = set(args.repo)
    excludes = set(args.exclude)

    repos = gitea_list_repos(gitea_api_url, args.owner, gitea_token)
    log(f"共发现 {len(repos)} 个仓库（{args.owner}）")

    updated, skipped, errors = [], [], []
    for repo in repos:
        name = repo.get("name", "")
        full_name = repo.get("full_name") or f"{args.owner}/{name}"
        if only and name not in only:
            continue
        if name in excludes:
            log(f"{full_name}: 已排除，跳过")
            skipped.append(full_name)
            continue
        if repo.get("mirror"):
            log(f"{full_name}: 镜像仓库，跳过")
            skipped.append(full_name)
            continue
        if repo.get("archived"):
            log(f"{full_name}: 已归档，跳过")
            skipped.append(full_name)
            continue
        if repo.get("empty"):
            log(f"{full_name}: 空仓库（无默认分支），跳过")
            skipped.append(full_name)
            continue

        default_branch = repo.get("default_branch") or "main"
        if gitea_file_exists(
            gitea_api_url, args.owner, name, WOODPECKER_FILE, default_branch, gitea_token
        ):
            log(f"{full_name}: 已存在 {WOODPECKER_FILE}，跳过")
            skipped.append(full_name)
            continue

        rendered = render_template(template_text, sync_image, secrets_mount)
        if args.dry_run:
            log(f"{full_name}: dry-run，将创建 {WOODPECKER_FILE}")
            updated.append(full_name)
            continue

        code, payload = gitea_create_file(
            gitea_api_url,
            args.owner,
            name,
            WOODPECKER_FILE,
            rendered,
            args.commit_message,
            default_branch,
            gitea_token,
        )
        if code == 201:
            log(f"{full_name}: {WOODPECKER_FILE} 创建成功")
            updated.append(full_name)
        else:
            # 并发场景下文件可能已由其他流程创建：复核一次
            if gitea_file_exists(
                gitea_api_url, args.owner, name, WOODPECKER_FILE, default_branch, gitea_token
            ):
                log(f"{full_name}: 文件已存在（可能由其他流程创建），视为成功")
                updated.append(full_name)
            else:
                reason = payload.get("message", payload) if isinstance(payload, dict) else payload
                log(f"{full_name}: 创建失败 (HTTP {code}): {reason}")
                errors.append(full_name)

    log("=" * 60)
    log(f"完成：新增/就绪 {len(updated)}，跳过 {len(skipped)}，失败 {len(errors)}")
    if errors:
        log(f"失败仓库: {', '.join(errors)}")
        return 1
    return 0


if __name__ == "__main__":
    main()

