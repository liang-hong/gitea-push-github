#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""provision/add_woodpecker_yml.py 单元测试（mock HTTP）。

运行：python3 -m unittest discover -s tests -v
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "provision"))
import add_woodpecker_yml as prov  # noqa: E402


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


def make_http_error_404():
    import io
    import urllib.error

    fp = io.BytesIO(json.dumps({"message": "Not Found"}).encode("utf-8"))
    return urllib.error.HTTPError("http://mock", 404, "not found", {}, fp)


def fake_urlopen(side_effect):
    return mock.patch.object(urllib.request, "urlopen", side_effect=side_effect)


def make_repo(name, **kwargs):
    data = {
        "name": name,
        "full_name": f"alice/{name}",
        "default_branch": "main",
        "empty": False,
        "mirror": False,
        "archived": False,
    }
    data.update(kwargs)
    return data


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.creds = os.path.join(self.tmp, "creds.env")
        with open(self.creds, "w", encoding="utf-8") as handle:
            handle.write(
                "GITEA_API_URL=https://git.example.com\n"
                "GITEA_TOKEN=gitea_tok\n"
                "SYNC_IMAGE=ghcr.io/octocat/gitea-github-sync:latest\n"
            )
        self.template = os.path.join(self.tmp, "woodpecker.yml")
        with open(self.template, "w", encoding="utf-8") as handle:
            handle.write(
                "image: {{SYNC_IMAGE}}\n"
                "volumes:\n"
                "  - {{SECRETS_MOUNT}}:/run/secrets/gitea-push-github:ro\n"
            )

    def test_adds_missing_file_to_all_repos(self):
        repos = [make_repo("repo-a"), make_repo("repo-b")]
        created = []

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/contents/.woodpecker.yml" in url:
                raise make_http_error_404()
            if request.method == "POST" and "/contents/.woodpecker.yml" in url:
                body = json.loads(request.data)
                content = base64.b64decode(body["content"]).decode("utf-8")
                self.assertIn("image: ghcr.io/octocat/gitea-github-sync:latest", content)
                self.assertIn("/home/test/secrets:/run/secrets/gitea-push-github:ro", content)
                self.assertEqual(body["branch"], "main")
                self.assertIn("ci: add woodpecker sync template", body["message"])
                created.append(url)
                return make_response(201, {"content": content})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = prov.main(
                ["--owner", "alice", "--credentials", self.creds,
                 "--template", self.template, "--secrets-mount", "/home/test/secrets"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(created), 2)

    def test_skips_existing_file(self):
        repos = [make_repo("repo-a")]
        created = []

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/contents/.woodpecker.yml" in url:
                return make_response(200, {"name": ".woodpecker.yml"})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = prov.main(
                ["--owner", "alice", "--credentials", self.creds,
                 "--template", self.template, "--secrets-mount", "/home/test/secrets"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(created, [])

    def test_skips_empty_archived_mirror(self):
        repos = [
            make_repo("empty-repo", empty=True),
            make_repo("archived-repo", archived=True),
            make_repo("mirror-repo", mirror=True),
        ]

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = prov.main(
                ["--owner", "alice", "--credentials", self.creds,
                 "--template", self.template, "--secrets-mount", "/home/test/secrets"]
            )
        self.assertEqual(rc, 0)

    def test_dry_run_does_not_create(self):
        repos = [make_repo("repo-a")]

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/contents/.woodpecker.yml" in url:
                raise make_http_error_404()
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = prov.main(
                ["--owner", "alice", "--credentials", self.creds,
                 "--template", self.template, "--secrets-mount", "/home/test/secrets",
                 "--dry-run"]
            )
        self.assertEqual(rc, 0)

    def test_secrets_mount_defaults_to_credentials_dir(self):
        repos = [make_repo("repo-a")]

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/contents/.woodpecker.yml" in url:
                raise make_http_error_404()
            if request.method == "POST" and "/contents/.woodpecker.yml" in url:
                body = json.loads(request.data)
                content = base64.b64decode(body["content"]).decode("utf-8")
                self.assertIn(f"{self.tmp}:/run/secrets/gitea-push-github:ro", content)
                return make_response(201, {"content": content})
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            rc = prov.main(
                ["--owner", "alice", "--credentials", self.creds,
                 "--template", self.template]
            )
        self.assertEqual(rc, 0)

    def test_missing_sync_image_fails(self):
        repos = [make_repo("repo-a")]
        creds = os.path.join(self.tmp, "creds-no-image.env")
        with open(creds, "w", encoding="utf-8") as handle:
            handle.write("GITEA_API_URL=https://git.example.com\nGITEA_TOKEN=gitea_tok\n")

        def handler(request, **kwargs):
            url = request.full_url
            if request.method == "GET" and "/users/alice/repos" in url:
                return make_response(200, repos)
            if request.method == "GET" and "/contents/.woodpecker.yml" in url:
                raise make_http_error_404()
            raise AssertionError(f"unexpected {request.method} {url}")

        with fake_urlopen(side_effect=handler):
            with self.assertRaises(SystemExit):
                prov.main(
                    ["--owner", "alice", "--credentials", creds,
                     "--template", self.template]
                )

    def test_render_template(self):
        text = "image: {{SYNC_IMAGE}}\nvolumes:\n  - {{SECRETS_MOUNT}}:/x:ro\n"
        out = prov.render_template(text, "img:1", "/srv/secrets")
        self.assertIn("image: img:1", out)
        self.assertIn("- /srv/secrets:/x:ro", out)


if __name__ == "__main__":
    unittest.main()

