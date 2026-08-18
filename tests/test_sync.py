#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync/gitea_github_sync.py 单元测试（mock HTTP，无需真实凭据与网络）。

运行：python3 -m unittest discover -s tests -v
"""

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
    def test_missing_config_is_disabled(self):
        self.assertIsNone(sync.load_repo_config("/nonexistent/.github-sync.yml"))
        self.assertFalse(sync.config_enabled(None))

    def test_min_parser(self):
        text = """# 注释
github:
  enabled: true
  private: false
"""
        cfg = sync._parse_yaml_min(text)
        self.assertTrue(cfg["github"]["enabled"])
        self.assertFalse(cfg["github"]["private"])

    def test_min_parser_inline_comment(self):
        text = "github:\n  enabled: true  # 启用\n  private: true\n"
        cfg = sync._parse_yaml_min(text)
        self.assertTrue(cfg["github"]["enabled"])

    def test_defaults_private_true(self):
        text = "github:\n  enabled: true\n"
        cfg = sync._parse_yaml_min(text)
        self.assertTrue(sync.config_enabled(cfg))
        self.assertTrue(sync.config_private(cfg))  # 默认私有

    def test_disabled_by_default(self):
        text = "github:\n  enabled: false\n"
        cfg = sync._parse_yaml_min(text)
        self.assertFalse(sync.config_enabled(cfg))


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

    def write_config(self, text):
        path = os.path.join(self.tmp, ".github-sync.yml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_disabled_does_not_call_api(self):
        config = self.write_config("github:\n  enabled: false\n")
        with fake_urlopen(side_effect=AssertionError("不应发起 API 调用")):
            rc = sync.main(
                ["--repo-owner", "alice", "--repo-name", "repo",
                 "--credentials", self.creds, "--config", config]
            )
        self.assertEqual(rc, 0)

    def test_missing_config_is_disabled(self):
        with fake_urlopen(side_effect=AssertionError("不应发起 API 调用")):
            rc = sync.main(
                ["--repo-owner", "alice", "--repo-name", "repo",
                 "--credentials", self.creds, "--config", "/nonexistent/.github-sync.yml"]
            )
        self.assertEqual(rc, 0)

    def test_enabled_creates_private_repo_and_mirror(self):
        config = self.write_config("github:\n  enabled: true\n")
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
                self.assertEqual(
                    body["remote_address"], "https://github.com/octocat/repo.git"
                )
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(
                ["--repo-owner", "alice", "--repo-name", "repo",
                 "--credentials", self.creds, "--config", config]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c.method == "POST"), 2)

    def test_private_false_creates_public(self):
        config = self.write_config("github:\n  enabled: true\n  private: false\n")

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                raise make_http_error(404, {"message": "Not Found"})
            if request.method == "POST" and url == "https://api.github.com/user/repos":
                body = json.loads(request.data)
                self.assertFalse(body["private"])
                return make_response(201, {"name": body["name"]})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(200, [])
            if request.method == "POST" and "push_mirrors" in url:
                return make_response(201, {})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(
                ["--repo-owner", "alice", "--repo-name", "repo",
                 "--credentials", self.creds, "--config", config]
            )
        self.assertEqual(rc, 0)

    def test_existing_repo_and_mirror_skipped(self):
        config = self.write_config("github:\n  enabled: true\n")

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/repos/octocat/repo" in url and "api.github.com" in url:
                return make_response(200, {"full_name": "octocat/repo"})
            if request.method == "GET" and "push_mirrors" in url:
                return make_response(
                    200, [{"remote_address": "https://github.com/octocat/repo.git"}]
                )
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = sync.main(
                ["--repo-owner", "alice", "--repo-name", "repo",
                 "--credentials", self.creds, "--config", config]
            )
        self.assertEqual(rc, 0)

    def test_missing_credentials_fails(self):
        config = self.write_config("github:\n  enabled: true\n")
        missing_creds = os.path.join(self.tmp, "missing.env")
        with self.assertRaises(SystemExit):
            sync.main(
                ["--repo-owner", "alice", "--repo-name", "repo",
                 "--credentials", missing_creds, "--config", config]
            )


if __name__ == "__main__":
    unittest.main()

