"""How lib/iso.py proves identity to a registry.

Two registries, two shapes, and getting this wrong stops a bake before it downloads
anything:

    ghcr.io      GET /token, then Bearer on every request; the package is public
    Forgejo      no /token endpoint at all - 404 - and Basic auth on every request

The first version only knew the first shape. Pointed at Forgejo it asked /token, took
the 404 as "the package is private", and refused to continue.
"""

import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import iso  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode() if not isinstance(self._payload, bytes) \
            else self._payload


class TokenRegistry(unittest.TestCase):
    """ghcr.io: a token endpoint that answers with a JSON token."""

    def setUp(self):
        self.auth = iso.Auth()
        self.env = mock.patch.dict(os.environ, {"ISO_USER": "", "ISO_PASSWORD": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_anonymous_token_is_used(self):
        # Public package: no credentials, and the request must not carry any.
        def fake_open(req, timeout=60):
            return FakeResponse({"token": "anon"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            headers = self.auth.headers("easydev-org/easydiskbake")

        self.assertEqual(headers["Authorization"], "Bearer anon")

    def test_it_asks_for_scope_pull(self):
        seen = []

        def fake_open(req, timeout=60):
            seen.append(req.full_url)
            return FakeResponse({"token": "t"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            self.auth.headers("owner/repo")

        self.assertTrue(any("scope=repository:owner/repo:pull" in u for u in seen), seen)


class BasicRegistry(unittest.TestCase):
    """Forgejo: /token is 404, and Basic auth goes on every request."""

    def setUp(self):
        self.auth = iso.Auth()
        self.env = mock.patch.dict(
            os.environ, {"ISO_USER": "fenjin", "ISO_PASSWORD": "secret"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_a_404_means_basic_auth(self):
        def fake_open(req, timeout=60):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            headers = self.auth.headers("fenjin/easydiskbake")

        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_it_decides_once(self):
        # Detecting on every request would double the calls to a registry that is
        # local but still not free.
        calls = []

        def fake_open(req, timeout=60):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            self.auth.headers("fenjin/easydiskbake")
            first = len(calls)
            self.auth.headers("fenjin/easydiskbake")

        self.assertEqual(len(calls), first)

    def test_no_credentials_means_no_header(self):
        # A registry that allows anonymous pushes still works.
        with mock.patch.dict(os.environ, {"ISO_USER": "", "ISO_PASSWORD": ""}):
            auth = iso.Auth()
            with mock.patch("urllib.request.urlopen",
                            side_effect=urllib.error.HTTPError("u", 404, "x", {}, None)):
                headers = auth.headers("fenjin/easydiskbake")
        self.assertEqual(headers, {})


class VendorUrls(unittest.TestCase):
    """A vendor URL is not this registry and must not carry its credentials."""

    def test_no_auth_header_is_added(self):
        added = []

        class FakeReq:
            def __init__(self, url, method=None, data=None, headers=None):
                self.full_url = url
                # _download now passes a User-Agent; the fake has to accept it or the
                # request is never created and this test cannot inspect anything.
                self.headers = dict(headers or {})
                added.append(self)

            def add_header(self, key, value):
                self.headers[key] = value

        # A download that fails is retried DOWNLOAD_ATTEMPTS times with a backoff; the
        # retrying is not what this test is about, and sleeping through it is 30 seconds
        # of a unit test suite.
        with mock.patch("urllib.request.Request", FakeReq), \
             mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("stop here")):
            try:
                iso._download("https://github.com/x/y.iso", "/tmp/x.iso",
                              scope="", expected_size=None)
            except Exception:
                pass

        self.assertTrue(added)
        self.assertNotIn("Authorization", getattr(added[0], "headers", {}))


if __name__ == "__main__":
    unittest.main()
