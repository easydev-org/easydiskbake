"""There is one source for each input, and a missing one is a failure.

Every fallback in this repository has been removed, and each removal was preceded by
the same experience: the fallback did not make the pipeline more robust, it made a
failure look like slow progress somewhere else.

    Windows ISO     package -> vendor        a missing image became a 7.2 GiB download
                                             that then failed on a later step
    virtio-win      package -> vendor        a missing CD became a warning, and setup
                                             stopped at a pane listing no disks
    aria2c          aria2c -> curl           a missing downloader became a transfer that
                                             died at 20 MiB with a message about a token

These tests pin the shape: the code has one path, and the error says what to run.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import iso  # noqa: E402


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def function_source(path: str, name: str) -> str:
    """The source of one function, so its branches can be inspected."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(open(path, encoding="utf-8").read(), node) or ""
    raise AssertionError(f"no function {name} in {path}")


class WindowsFetchHasNoVendorPath(unittest.TestCase):
    """windows/11/bake.py pulls from ghcr only."""

    path = os.path.join(REPO, "windows", "11", "bake.py")

    def test_fetch_iso_does_not_call_fetch_url(self):
        # fetch_url is the vendor download. It belongs to publish_iso.py, which is the
        # one place that should ever talk to Microsoft.
        src = function_source(self.path, "fetch_iso")
        self.assertNotIn("fetch_url", src)
        self.assertIn("fetch_iso", src)

    def test_fetch_virtio_does_not_call_fetch_url(self):
        src = function_source(self.path, "fetch_virtio")
        self.assertNotIn("fetch_url", src)

    def test_fetch_virtio_is_not_optional(self):
        # It used to return None on failure and the caller omitted the CD. Setup cannot
        # see a virtio disk without the drivers, so "no CD" is not a degraded mode.
        src = function_source(self.path, "fetch_virtio")
        self.assertNotIn("return None", src)
        self.assertNotIn("WARNING", src)
        # The signature says str, not str | None.
        self.assertIn("def fetch_virtio(self) -> str:", src)

    def test_the_error_says_what_to_run(self):
        for name in ("fetch_iso", "fetch_virtio"):
            with self.subTest(fn=name):
                src = function_source(self.path, name)
                self.assertIn("publish-iso.yml", src)


class MacosFetchHasNoCurlPath(unittest.TestCase):
    """lib/macos_recovery.py downloads with aria2c only."""

    path = os.path.join(REPO, "lib", "macos_recovery.py")

    def test_no_curl_fallback(self):
        src = function_source(self.path, "_fetch")
        self.assertNotIn('"curl"', src)
        self.assertNotIn("falling back", src)

    def test_missing_aria2c_is_an_error(self):
        src = function_source(self.path, "_fetch")
        self.assertIn("RecoveryError", src)
        self.assertIn("aria2c is not installed", src)


class OnlyThePublisherTalksToVendors(unittest.TestCase):
    """fetch_url exists, and only publish_iso.py calls it."""

    def test_callers_of_fetch_url(self):
        callers = []
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "tests")]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                # lib/iso.py defines it and also calls it from its __main__ branch,
                # so the definition file is a legitimate caller; everything else is
                # not.
                if "fetch_url(" not in text:
                    continue
                calls = text.count("fetch_url(")
                defines = text.count("def fetch_url(")
                if calls > defines:
                    callers.append(os.path.relpath(path, REPO))

        # lib/iso.py defines it and has a __main__ branch; publish_iso.py uses it.
        # Nothing else may.
        self.assertEqual(sorted(callers), ["lib/iso.py", "lib/publish_iso.py"], callers)


class TagsLookupIsNotSwallowed(unittest.TestCase):
    """lib/ci.py raises on a failed lookup instead of returning an empty list."""

    def test_no_bare_except(self):
        src = function_source(os.path.join(REPO, "lib", "ci.py"), "tags_in_package")
        self.assertNotIn("except Exception", src)
        self.assertIn("Failed", src)

    def test_workflows_do_not_mask_the_failure(self):
        # `|| true` after the call turned a broken lookup into "no tags".
        for name in ("bake-windows.yml", "publish-iso.yml"):
            path = os.path.join(REPO, ".github", "workflows", name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("ci.py tags --prefix windows-base- || true", text)
            self.assertNotIn("ci.py tags --prefix iso- || true", text)


if __name__ == "__main__":
    unittest.main()
