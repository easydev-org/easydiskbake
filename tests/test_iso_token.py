"""The registry token: anonymous first, credentials only if asked for.

The package this repository publishes is public, so ghcr hands out a pull token to
anyone. The first version of _token() required GITHUB_TOKEN unconditionally and sent
Basic auth - correct for a private package, fatal on any runner that does not set that
variable. Forgejo is one:

    urllib.error.HTTPError: HTTP Error 403: Forbidden
    File "lib/iso.py", line 89, in _token
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
        return json.dumps(self._payload).encode()


class Token(unittest.TestCase):

    def test_anonymous_when_no_credentials_are_set(self):
        # The case that failed: a runner with no GITHUB_TOKEN and a public package.
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_TOKEN", "GITHUB_ACTOR")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse({"token": "anon-token"})) as open_:
            self.assertEqual(iso._token("easydev-org/easydiskbake"), "anon-token")

        # No Authorization header on the anonymous attempt.
        request = open_.call_args[0][0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_credentials_are_used_when_anonymous_is_refused(self):
        # A private package: the first request gets 401, the second carries Basic auth.
        calls = []

        def fake_open(req, timeout=60):
            calls.append(req)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
            return FakeResponse({"token": "private-token"})

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_x", "GITHUB_ACTOR": "me"}), \
             mock.patch("urllib.request.urlopen", side_effect=fake_open):
            self.assertEqual(iso._token("easydev-org/easydiskbake"), "private-token")

        self.assertEqual(len(calls), 2)
        auth = calls[1].get_header("Authorization")
        self.assertTrue(auth.startswith("Basic "))

    def test_a_private_package_without_credentials_says_so(self):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 401, "x", {}, None)):
            with self.assertRaises(iso.IsoError) as ctx:
                iso._token("easydev-org/easydiskbake")
        self.assertIn("needs credentials", str(ctx.exception))

    def test_a_non_auth_error_is_not_retried_with_credentials(self):
        # A 500 is not a permissions problem; falling through to a second request would
        # just make the same failure twice.
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 500, "boom", {}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                iso._token("easydev-org/easydiskbake")


if __name__ == "__main__":
    unittest.main()
