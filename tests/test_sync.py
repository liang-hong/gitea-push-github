#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync/gitea_github_sync.py 单元测试（mock HTTP，无需真实凭据与网络）。

运行：python3 -m unittest discover -s tests -v
"""

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sync"))
import gitea_github_sync as sync  # noqa: E402


def make_response(code, payload):
    class _Resp:
        status = code

        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp()


def make_http_error(code, payload):
    fp = io.BytesIO(json.dumps(payload).encode("utf-8"))
    return urllib.error.HTTPError("http://mock", code, "mock error", {}, fp)


def fake_urlopen(side_effect):
    return mock.patch.object(urllib.request, "urlopen", side_effect=side_effect)


def encode_file(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def with_api_config(base_handler, branches=("main",), repo_texts=None):
    """包装 base_handler，注入 Gitea 分支列表与各分支 .github-sync.yml 读取响应。

    repo_texts: {repo_name: {branch: 内容}}；未列出的 (repo, branch) 视为缺失(404)。
    """
    repo_texts = repo_texts or {}

    def handler(request, **kwargs):
        url = request.full_url
        if request.method == "GET" and "/branches" in url and "git.example.com" in url:
            return make_response(200, [{"name": b} for b in branches])
        if (
            request.method == "GET"
            and "/contents/.github-sync.yml" in url
            and "git.example.com" in url
        ):
            repo = url.split("/repos/")[-1].split("/contents/")[0].split("/", 1)[-1]
            branch = url.split("ref=")[-1] if "ref=" in url else None
            text = (repo_texts.get(repo) or {}).get(branch)
            if text is None:
                raise make_http_error(404, {"message": "Not Found"})
            return make_response(200, {"content": encode_file(text)})
        return base_handler(request, **kwargs)

    return handler


class CredentialsTest(unittest.TestCase):
    def test_load_dotenv(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("# 注释\nGITHUB_USERNAME=octocat\nGITEA_TOKEN= xxxx \n")
            path = handle.name
        try:
            values = sync.load_dotenv(path)
            self.assertEqual(values["GITHUB_USERNAME"], "octocat")
            self.assertEqual(values["GITEA_TOKEN"], "xxxx")
        finally:
            os.unlink(path)

    def test_resolve_credentials_env_fallback(self):
        env = {
            "GITHUB_USERNAME": "octocat",
            "GITHUB_TOKEN": "github_pat_1",
            "GITEA_API_URL": "https://git.example.com",
            "GITEA_TOKEN": "gitea_tok",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(sync, "DEFAULT_CREDENTIALS", "/nonexistent/path"):
                creds = sync.resolve_credentials()
        self.assertEqual(creds, env)

    def test_resolve_credentials_from_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("GITHUB_TOKEN=from_file\n")
            path = handle.name
        try:
            creds = sync.resolve_credentials(path)
            self.assertEqual(creds["GITHUB_TOKEN"], "from_file")
        finally:
            os.unlink(path)


class ConfigTest(unittest.TestCase):
    def test_missing_config_is_default_remove(self):
        self.assertEqual(sync.config_state(None), "remove")
        self.assertEqual(sync.config_state({}), "remove")

    def test_min_parser(self):
        text = """# 注释\ngithub:\n  state: enable\n  private: false\n"""
        cfg = sync.parse_repo_config_text(text)
        self.assertEqual(sync.config_state(cfg), "enable")
        self.assertFalse(cfg["github"]["private"])

    def test_min_parser_inline_comment(self):
        text = "github:\n  state: suspend  # 暂停\n  private: true\n"
        cfg = sync.parse_repo_config_text(text)
        self.assertEqual(sync.config_state(cfg), "suspend")

    def test_defaults_private_true(self):
        text = "github:\n  state: enable\n"
        cfg = sync.parse_repo_config_text(text)
        self.assertEqual(sync.config_state(cfg), "enable")
        self.assertTrue(sync.config_private(cfg))

    def test_state_defaults_remove(self):
        cfg = sync.parse_repo_config_text("github:\n  private: true\n")
        self.assertEqual(sync.config_state(cfg), "remove")

    def test_state_disable_equals_remove(self):
        cfg = sync.parse_repo_config_text("github:\n  state: disable\n")
        self.assertEqual(sync.config_state(cfg), "remove")

    def test_state_case_insensitive(self):
        cfg = sync.parse_repo_config_text("github:\n  state: ENABLE\n")
        self.assertEqual(sync.config_state(cfg), "enable")

    def test_default_branch_none_by_default(self):
        cfg = sync.parse_repo_config_text("github:\n  state: enable\n")
        self.assertIsNone(sync.config_default_branch(cfg))

    def test_default_branch(self):
        cfg = sync.parse_repo_config_text("github:\n  state: enable\n  default_branch: no-ci\n")
        self.assertEqual(sync.config_default_branch(cfg), "no-ci")

    def test_illegal_state_raises(self):
        cfg = sync.parse_repo_config_text("github:\n  state: foo\n")
        with self.assertRaises(ValueError):
            sync.config_state(cfg)


class RepoConfigApiTest(unittest.TestCase):
    GITEA = "https://git.example.com"

    def _call(self, branches, contents):
        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/branches" in url and self.GITEA in url:
                return make_response(200, [{"name": b} for b in branches])
            if request.method == "GET" and "/contents/.github-sync.yml" in url and self.GITEA in url:
                branch = url.split("ref=")[-1] if "ref=" in url else None
                text = contents.get(branch)
                if text is None:
                    raise make_http_error(404, {"message": "Not Found"})
                return make_response(200, {"content": encode_file(text)})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            return sync.load_repo_config_via_api(self.GITEA, "alice", "repo", "tok")

    def test_all_branches_identical(self):
        text = "github:\n  state: enable\n"
        cfg = self._call(["main", "dev"], {"main": text, "dev": text})
        self.assertEqual(sync.config_state(cfg), "enable")

    def test_missing_on_any_branch_disables(self):
        cfg = self._call(["main", "dev"], {"main": "github:\n  state: enable\n", "dev": None})
        self.assertIsNone(cfg)

    def test_content_differs_disables(self):
        cfg = self._call(["main", "dev"], {
            "main": "github:\n  state: enable\n",
            "dev": "github:\n  state: suspend\n",
        })
        self.assertIsNone(cfg)

    def test_comments_only_differs_ok(self):
        cfg = self._call(["main", "dev"], {
            "main": "github:\n  state: enable  # 主分支\n",
            "dev": "github:\n  state: enable\n",
        })
        self.assertEqual(sync.config_state(cfg), "enable")

    def test_no_branches_disables(self):
        self.assertIsNone(self._call([], {}))

    def test_branches_list_failure_disables(self):
        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/branches" in url and self.GITEA in url:
                raise make_http_error(500, {"message": "boom"})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            cfg = sync.load_repo_config_via_api(self.GITEA, "alice", "repo", "tok")
        self.assertIsNone(cfg)



class SyncMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.creds = os.path.join(self.tmp, "creds.env")
        with open(self.creds, "w", encoding="utf-8") as handle:
            handle.write(
                "GITHUB_USERNAME=octocat\n"
                "GITHUB_TOKEN=github_pat_1\n"
                "GITEA_API_URL=https://git.example.com\n"
                "GITEA_TOKEN=gitea_tok\n"
            )
        self.patch_default = mock.patch.object(sync, "DEFAULT_CREDENTIALS", "/nonexistent/path")
        self.patch_default.start()

    def tearDown(self):
        self.patch_default.stop()

    def base_args(self):
        return ["--repo-owner", "alice", "--credentials", self.creds]

    def test_single_repo_remove_disable_noop(self):
        for state in ("remove", "disable"):
            def base_handler(request, **kwargs):
                raise AssertionError(f"不应发起 API 调用: {request.method} {request.full_url}")

            handler = with_api_config(
                base_handler,
                repo_texts={"repo": {"main": f"github:\n  state: {state}\n"}},
            )
            with fake_urlopen(side_effect=handler):
                rc = sync.main(self.base_args() + ["--repo-name", "repo"])
            self.assertEqual(rc, 0)

    def test_single_repo_enable_creates_private(self):
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "POST" and url == "https://api.github.com/user/repos":
                body = json.loads(request.data)
                self.assertTrue(body["private"])
                return make_response(201, {"name": body["name"]})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                body = json.loads(request.data)
                self.assertEqual(body["remote_address"], "https://github.com/octocat/repo.git")
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: enable\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "POST"), 2)

    def test_single_repo_existing_skipped(self):
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                return make_response(200, {"private": True})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [{"remote_address": "https://github.com/octocat/repo.git"}])
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: enable\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "PATCH"), 0)

    def test_single_repo_existing_visibility_mismatch_patches(self):
        # 仓库已存在且可见性与配置不一致（私有->公开，需显式 private: false）
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and url.endswith("/repos/octocat/repo") and "api.github.com" in url:
                return make_response(200, {"private": True})
            if request.method == "PATCH" and url.endswith("/repos/octocat/repo"):
                body = json.loads(request.data)
                self.assertFalse(body["private"])
                return make_response(200, {"private": False})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler,
            repo_texts={"repo": {"main": "github:\n  state: enable\n  private: false\n"}},
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "PATCH"), 1)

    def test_single_repo_existing_public_forced_private(self):
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and url.endswith("/repos/octocat/repo") and "api.github.com" in url:
                return make_response(200, {"private": False})
            if request.method == "PATCH" and url.endswith("/repos/octocat/repo"):
                body = json.loads(request.data)
                self.assertTrue(body["private"])
                return make_response(200, {"private": True})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: enable\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "PATCH"), 1)

    def test_single_repo_visibility_mismatch_dry_run_no_patch(self):
        def base_handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and url.endswith("/repos/octocat/repo") and "api.github.com" in url:
                return make_response(200, {"private": True})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler,
            repo_texts={"repo": {"main": "github:\n  state: enable\n  private: false\n"}},
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--dry-run"])
        self.assertEqual(rc, 0)

    def test_single_repo_enable_sets_default_branch(self):
        config_text = "github:\n  state: enable\n  default_branch: no-ci\n"
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and url.endswith("/repos/octocat/repo") and "api.github.com" in url:
                return make_response(200, {"private": True, "default_branch": "main"})
            if request.method == "GET" and "/branches/no-ci" in url and "api.github.com" in url:
                return make_response(200, {"name": "no-ci"})
            if request.method == "PATCH" and url.endswith("/repos/octocat/repo"):
                self.assertEqual(json.loads(request.data), {"default_branch": "no-ci"})
                return make_response(200, {"private": True, "default_branch": "no-ci"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(base_handler, repo_texts={"repo": {"main": config_text}})
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "PATCH"), 1)

    def test_single_repo_enable_default_branch_missing_skips(self):
        config_text = "github:\n  state: enable\n  default_branch: no-ci\n"
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and url.endswith("/repos/octocat/repo") and "api.github.com" in url:
                return make_response(200, {"private": True, "default_branch": "main"})
            if request.method == "GET" and "/branches/no-ci" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(base_handler, repo_texts={"repo": {"main": config_text}})
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "PATCH"), 0)

    def test_single_repo_enable_default_branch_combined_with_private(self):
        config_text = "github:\n  state: enable\n  private: true\n  default_branch: no-ci\n"
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and url.endswith("/repos/octocat/repo") and "api.github.com" in url:
                return make_response(200, {"private": False, "default_branch": "main"})
            if request.method == "GET" and "/branches/no-ci" in url and "api.github.com" in url:
                return make_response(200, {"name": "no-ci"})
            if request.method == "PATCH" and url.endswith("/repos/octocat/repo"):
                self.assertEqual(json.loads(request.data), {"private": True, "default_branch": "no-ci"})
                return make_response(200, {"private": True, "default_branch": "no-ci"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(base_handler, repo_texts={"repo": {"main": config_text}})
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "PATCH"), 1)

    def test_single_repo_default_branch_dry_run_no_patch(self):
        config_text = "github:\n  state: enable\n  default_branch: no-ci\n"

        def base_handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and url.endswith("/repos/octocat/repo") and "api.github.com" in url:
                return make_response(200, {"private": True, "default_branch": "main"})
            if request.method == "GET" and "/branches/no-ci" in url and "api.github.com" in url:
                return make_response(200, {"name": "no-ci"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(base_handler, repo_texts={"repo": {"main": config_text}})
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--dry-run"])
        self.assertEqual(rc, 0)

    def test_single_repo_suspend_deletes_matching_mirror(self):
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [{
                    "remote_address": "https://github.com/octocat/repo.git",
                    "remote_name": "push_mirror_1",
                }])
            if request.method == "DELETE" and url.endswith("/push_mirrors/push_mirror_1"):
                return make_response(204, None)
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: suspend\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "DELETE"), 1)

    def test_single_repo_suspend_no_mirror_ok(self):
        calls = []

        def base_handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: suspend\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "DELETE"), 0)

    def test_single_repo_suspend_nonmatching_mirror_untouched(self):
        def base_handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [{
                    "remote_address": "https://gitlab.com/other/repo.git",
                    "remote_name": "other_mirror",
                }])
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: suspend\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)

    def test_single_repo_illegal_state_fails(self):
        def base_handler(request, **kwargs):
            raise AssertionError(f"不应发起 API 调用: {request.method} {request.full_url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: foo\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 1)

    def test_single_repo_enable_dry_run_no_mutation(self):
        def base_handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: enable\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--dry-run"])
        self.assertEqual(rc, 0)

    def test_single_repo_suspend_dry_run_no_delete(self):
        def base_handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [{
                    "remote_address": "https://github.com/octocat/repo.git",
                    "remote_name": "push_mirror_1",
                }])
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(
            base_handler, repo_texts={"repo": {"main": "github:\n  state: suspend\n"}}
        )
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--dry-run"])
        self.assertEqual(rc, 0)

    def test_single_repo_config_missing_disables(self):
        def base_handler(request, **kwargs):
            raise AssertionError(f"不应发起 API 调用: {request.method} {request.full_url}")

        handler = with_api_config(base_handler, repo_texts={"repo": {}})
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)

    def test_single_repo_config_multi_branch_inconsistent_disables(self):
        def base_handler(request, **kwargs):
            raise AssertionError(f"不应发起 API 调用: {request.method} {request.full_url}")

        handler = with_api_config(base_handler, branches=("main", "dev"), repo_texts={
            "repo": {
                "main": "github:\n  state: enable\n",
                "dev": "github:\n  state: suspend\n",
            },
        })
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)

    def test_scan_all_skips_special_and_disabled(self):
        repos = [
            {"name": "empty-repo", "empty": True, "archived": False, "mirror": False},
            {"name": "archived-repo", "empty": False, "archived": True, "mirror": False},
            {"name": "mirror-repo", "empty": False, "archived": False, "mirror": True},
            {"name": "disabled-repo", "empty": False, "archived": False, "mirror": False},
        ]

        def base_handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            raise AssertionError(f"unexpected {request.method} {url}")

        # disabled-repo 无配置(404) -> disable
        handler = with_api_config(base_handler, repo_texts={"disabled-repo": {}})
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args())
        self.assertEqual(rc, 0)

    def test_scan_all_creates_for_enabled(self):
        repos = [
            {"name": "disabled-repo", "empty": False, "archived": False, "mirror": False},
            {"name": "enabled-repo", "empty": False, "archived": False, "mirror": False},
        ]

        def base_handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/repos/octocat/enabled-repo" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "POST" and url == "https://api.github.com/user/repos":
                return make_response(201, {"name": json.loads(request.data)["name"]})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        handler = with_api_config(base_handler, repo_texts={
            "enabled-repo": {"main": "github:\n  state: enable\n"},
            "disabled-repo": {},
        })
        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args())
        self.assertEqual(rc, 0)

    def test_missing_owner_fails(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                sync.main(["--credentials", self.creds])

    def test_missing_credentials_fails(self):
        missing_creds = os.path.join(self.tmp, "missing.env")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                sync.main(
                    ["--repo-owner", "alice", "--repo-name", "repo",
                     "--credentials", missing_creds]
                )


if __name__ == "__main__":
    unittest.main()
