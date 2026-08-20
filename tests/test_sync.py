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
        self.assertIsNone(sync.load_repo_config("/nonexistent/.github-sync.yml"))
        self.assertEqual(sync.config_state(None), "remove")

    def test_min_parser(self):
        text = """# 注释
github:
  state: enable
  private: false
"""
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
        self.assertTrue(sync.config_private(cfg))  # 默认私有

    def test_state_defaults_remove(self):
        cfg = sync.parse_repo_config_text("github:\n  private: true\n")
        self.assertEqual(sync.config_state(cfg), "remove")

    def test_state_disable_equals_remove(self):
        cfg = sync.parse_repo_config_text("github:\n  state: disable\n")
        self.assertEqual(sync.config_state(cfg), "remove")

    def test_state_case_insensitive(self):
        cfg = sync.parse_repo_config_text("github:\n  state: ENABLE\n")
        self.assertEqual(sync.config_state(cfg), "enable")

    def test_illegal_state_raises(self):
        cfg = sync.parse_repo_config_text("github:\n  state: foo\n")
        with self.assertRaises(ValueError):
            sync.config_state(cfg)


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
        # 避免读取到本机真实默认凭据文件
        self.patch_default = mock.patch.object(sync, "DEFAULT_CREDENTIALS", "/nonexistent/path")
        self.patch_default.start()

    def tearDown(self):
        self.patch_default.stop()

    def write_config(self, text):
        path = os.path.join(self.tmp, ".github-sync.yml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def base_args(self):
        return ["--repo-owner", "alice", "--credentials", self.creds]

    def test_single_repo_remove_disable_noop(self):
        for state in ("remove", "disable"):
            config = self.write_config(f"github:\n  state: {state}\n")
            with fake_urlopen(side_effect=AssertionError("不应发起 API 调用")):
                rc = sync.main(self.base_args() + ["--repo-name", "repo", "--config", config])
            self.assertEqual(rc, 0)

    def test_single_repo_enabled_local_config_creates_private(self):
        config = self.write_config("github:\n  state: enable\n")
        calls = []

        def handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "POST" and url == "https://api.github.com/user/repos":
                body = json.loads(request.data)
                self.assertTrue(body["private"])  # 默认私有
                return make_response(201, {"name": body["name"]})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                body = json.loads(request.data)
                self.assertEqual(body["remote_address"], "https://github.com/octocat/repo.git")
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--config", config])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "POST"), 2)

    def test_single_repo_existing_skipped(self):
        config = self.write_config("github:\n  state: enable\n")

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                return make_response(200, {"full_name": "octocat/repo"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [{"remote_address": "https://github.com/octocat/repo.git"}])
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--config", config])
        self.assertEqual(rc, 0)

    def test_single_repo_suspend_deletes_matching_mirror(self):
        config = self.write_config("github:\n  state: suspend\n")
        calls = []

        def handler(request, **kwargs):
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

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--config", config])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "DELETE"), 1)

    def test_single_repo_suspend_no_mirror_ok(self):
        config = self.write_config("github:\n  state: suspend\n")
        calls = []

        def handler(request, **kwargs):
            calls.append(request)
            url = request.full_url
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--config", config])
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "DELETE"), 0)

    def test_single_repo_suspend_nonmatching_mirror_untouched(self):
        config = self.write_config("github:\n  state: suspend\n")

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [{
                    "remote_address": "https://gitlab.com/other/repo.git",
                    "remote_name": "other_mirror",
                }])
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--config", config])
        self.assertEqual(rc, 0)

    def test_single_repo_illegal_state_fails(self):
        config = self.write_config("github:\n  state: foo\n")
        with fake_urlopen(side_effect=AssertionError("不应发起 API 调用")):
            rc = sync.main(self.base_args() + ["--repo-name", "repo", "--config", config])
        self.assertEqual(rc, 1)

    def test_single_repo_enable_dry_run_no_mutation(self):
        config = self.write_config("github:\n  state: enable\n")

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(
                self.base_args() + ["--repo-name", "repo", "--config", config, "--dry-run"]
            )
        self.assertEqual(rc, 0)

    def test_single_repo_suspend_dry_run_no_delete(self):
        config = self.write_config("github:\n  state: suspend\n")

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [{
                    "remote_address": "https://github.com/octocat/repo.git",
                    "remote_name": "push_mirror_1",
                }])
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(
                self.base_args() + ["--repo-name", "repo", "--config", config, "--dry-run"]
            )
        self.assertEqual(rc, 0)

    def test_single_repo_config_via_api_disabled(self):
        # 无 --config：通过 Gitea API 读取 .github-sync.yml，404 = 未启用
        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/contents/.github-sync.yml" in url:
                raise make_http_error(404, {"message": "Not Found"})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--repo-name", "repo"])
        self.assertEqual(rc, 0)

    def test_single_repo_config_via_api_enabled(self):
        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/contents/.github-sync.yml" in url:
                return make_response(200, {"content": encode_file("github:\n  state: enable\n")})
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "POST" and url == "https://api.github.com/user/repos":
                body = json.loads(request.data)
                self.assertTrue(body["private"])
                return make_response(201, {"name": body["name"]})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

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

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/contents/.github-sync.yml" in url:
                # disabled-repo 无配置 -> 404
                raise make_http_error(404, {"message": "Not Found"})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args())
        self.assertEqual(rc, 0)

    def test_scan_all_creates_for_enabled(self):
        repos = [
            {"name": "disabled-repo", "empty": False, "archived": False, "mirror": False},
            {"name": "enabled-repo", "empty": False, "archived": False, "mirror": False},
        ]

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/contents/.github-sync.yml" in url:
                if "enabled-repo" in url:
                    return make_response(200, {"content": encode_file("github:\n  state: enable\n")})
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "GET" and "/repos/octocat/enabled-repo" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "POST" and url == "https://api.github.com/user/repos":
                return make_response(201, {"name": json.loads(request.data)["name"]})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args())
        self.assertEqual(rc, 0)

    def test_scan_all_ignores_local_config(self):
        # 全量模式必须逐仓库经 Gitea API 读配置；传 --config 不能覆盖全部仓库
        repos = [{"name": "repo-a", "empty": False, "archived": False, "mirror": False}]
        local = self.write_config("github:\n  state: remove\n")  # 若被应用则全部无操作
        created = []

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/contents/.github-sync.yml" in url:
                return make_response(200, {"content": encode_file("github:\n  state: enable\n")})
            if request.method == "GET" and "/repos/octocat/repo-a" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "POST" and url == "https://api.github.com/user/repos":
                created.append(url)
                return make_response(201, {"name": "repo-a"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(self.base_args() + ["--config", local])
        self.assertEqual(rc, 0)
        # --config 被忽略：repo-a 经 API 读到 state=enable，应发起建库 POST
        self.assertEqual(len(created), 1)

    def test_missing_owner_fails(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                sync.main(["--credentials", self.creds])

    def test_missing_credentials_fails(self):
        config = self.write_config("github:\n  state: enable\n")
        missing_creds = os.path.join(self.tmp, "missing.env")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                sync.main(
                    ["--repo-owner", "alice", "--repo-name", "repo",
                     "--credentials", missing_creds, "--config", config]
                )


if __name__ == "__main__":
    unittest.main()

