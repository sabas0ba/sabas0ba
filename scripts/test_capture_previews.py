#!/usr/bin/env python3
"""Unit tests for capture_previews.py.

Network access and the browser subprocess are mocked; the parsing, manifest and
orchestration logic runs for real against a temporary directory.
Run with: python -m unittest discover -s scripts
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import capture_previews as cp


@dataclass(frozen=True)
class FakeRepo:
    """Stand-in for build_index.Repo: capture only needs these four fields."""

    name: str = "proj"
    homepage: str = "https://u.github.io/proj/"
    updated_at: str = "2025-01-01T00:00:00Z"
    preview_url: str = ""


PAGE = """
<html><head>
  <meta property="og:image" content="{og}">
</head><body>
  <img src="{img}">
</body></html>
"""


class FindPageAnimationTest(unittest.TestCase):
    def test_prefers_declared_social_image(self):
        page = PAGE.format(og="demo.gif", img="other.gif")
        self.assertEqual(
            cp.find_page_animation(page, "https://u.github.io/proj/"),
            "https://u.github.io/proj/demo.gif",
        )

    def test_falls_back_to_embedded_image(self):
        page = PAGE.format(og="card.png", img="assets/loop.gif")
        self.assertEqual(
            cp.find_page_animation(page, "https://u.github.io/proj/"),
            "https://u.github.io/proj/assets/loop.gif",
        )

    def test_resolves_absolute_paths_against_the_page(self):
        page = '<img src="/proj/anim.apng">'
        self.assertEqual(
            cp.find_page_animation(page, "https://u.github.io/proj/index.html"),
            "https://u.github.io/proj/anim.apng",
        )

    def test_reads_srcset_when_src_is_absent(self):
        page = '<source srcset="a.gif 1x, b.gif 2x">'
        self.assertTrue(
            cp.find_page_animation(page, "https://u.github.io/proj/").endswith("a.gif")
        )

    def test_ignores_still_images(self):
        page = PAGE.format(og="card.png", img="logo.svg")
        self.assertEqual(cp.find_page_animation(page, "https://u.github.io/proj/"), "")

    def test_ignores_third_party_hosts(self):
        page = '<img src="https://cdn.example/tracker.gif">'
        self.assertEqual(cp.find_page_animation(page, "https://u.github.io/proj/"), "")

    def test_allows_github_asset_domain(self):
        page = '<img src="https://raw.githubusercontent.com/u/proj/main/demo.gif">'
        self.assertEqual(
            cp.find_page_animation(page, "https://u.github.io/proj/"),
            "https://raw.githubusercontent.com/u/proj/main/demo.gif",
        )

    def test_ignores_insecure_urls(self):
        page = '<img src="http://u.github.io/proj/demo.gif">'
        self.assertEqual(cp.find_page_animation(page, "https://u.github.io/proj/"), "")

    def test_handles_empty_page(self):
        self.assertEqual(cp.find_page_animation("", "https://u.github.io/proj/"), "")


class SafeStemTest(unittest.TestCase):
    def test_keeps_normal_repository_names(self):
        self.assertEqual(cp.safe_stem("font8x16-workbench"), "font8x16-workbench")
        self.assertEqual(cp.safe_stem("dowel_examples"), "dowel_examples")

    def test_strips_path_traversal(self):
        self.assertEqual(cp.safe_stem("../../etc/passwd"), "etc-passwd")
        self.assertNotIn("/", cp.safe_stem("a/b"))

    def test_never_returns_empty(self):
        self.assertEqual(cp.safe_stem("..."), "project")


class ManifestTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "manifest.json"
            cp.save_manifest(path, {"a": {"file": "a.png"}})
            self.assertEqual(cp.load_manifest(path), {"a": {"file": "a.png"}})

    def test_missing_or_broken_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cp.load_manifest(Path(d) / "nope.json"), {})
            broken = Path(d) / "broken.json"
            broken.write_text("[not a dict", encoding="utf-8")
            self.assertEqual(cp.load_manifest(broken), {})

    def test_is_current_requires_matching_revision_and_file(self):
        repo = FakeRepo()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "proj.png").write_bytes(b"x")
            fresh = {"updated_at": repo.updated_at, "file": "proj.png"}
            self.assertTrue(cp.is_current(fresh, repo, out))
            self.assertFalse(cp.is_current(None, repo, out))
            self.assertFalse(
                cp.is_current({"updated_at": "older", "file": "proj.png"}, repo, out)
            )
            self.assertFalse(
                cp.is_current({"updated_at": repo.updated_at, "file": "gone.png"}, repo, out)
            )

    def test_prune_removes_unreferenced_files(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            for name in ("keep.png", "stale.png", cp.MANIFEST_NAME):
                (out / name).write_bytes(b"x")
            removed = cp.prune(out, {"keep": {"file": "keep.png"}})
            self.assertEqual(removed, ["stale.png"])
            self.assertTrue((out / "keep.png").exists())
            self.assertTrue((out / cp.MANIFEST_NAME).exists())


class FindBrowserTest(unittest.TestCase):
    def test_prefers_explicit_path(self):
        with mock.patch.object(cp.shutil, "which", side_effect=lambda c: f"/bin/{c}"):
            self.assertEqual(cp.find_browser("my-chrome"), "/bin/my-chrome")

    def test_returns_empty_when_nothing_is_installed(self):
        with mock.patch.object(cp.shutil, "which", return_value=None), \
                mock.patch.dict(cp.os.environ, {}, clear=True), \
                mock.patch.object(cp.os.path, "isfile", return_value=False):
            self.assertEqual(cp.find_browser(), "")


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "previews"

    def _capture(self, repos, **kwargs):
        kwargs.setdefault("browser", "/usr/bin/chrome")
        return cp.capture(repos, self.out, **kwargs)

    def test_downloads_an_animation_found_in_the_page(self):
        page = '<img src="demo.gif">'
        with mock.patch.object(cp, "fetch_page", return_value=page), \
                mock.patch.object(cp, "download_image", return_value=True) as download, \
                mock.patch.object(cp, "screenshot") as shot, \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            (repo,) = self._capture([FakeRepo()])
        self.assertEqual(repo.preview_url, "previews/proj.gif")
        self.assertEqual(download.call_args[0][0], "https://u.github.io/proj/demo.gif")
        shot.assert_not_called()

    def test_screenshots_when_the_page_has_no_animation(self):
        with mock.patch.object(cp, "fetch_page", return_value="<html></html>"), \
                mock.patch.object(cp, "screenshot", return_value=True) as shot, \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            (repo,) = self._capture([FakeRepo()])
        self.assertEqual(repo.preview_url, "previews/proj.png")
        self.assertEqual(shot.call_args[0][1], "https://u.github.io/proj/")

    def test_leaves_repository_supplied_previews_alone(self):
        repo = FakeRepo(preview_url="https://raw.githubusercontent.com/u/p/main/a.gif")
        with mock.patch.object(cp, "fetch_page") as fetch:
            (result,) = self._capture([repo])
        self.assertEqual(result, repo)
        fetch.assert_not_called()

    def test_unreachable_page_leaves_the_card_without_an_image(self):
        with mock.patch.object(cp, "fetch_page", return_value=""), \
                mock.patch.object(cp, "screenshot") as shot, \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            (repo,) = self._capture([FakeRepo()])
        self.assertEqual(repo.preview_url, "")
        shot.assert_not_called()

    def test_without_a_browser_only_page_animations_are_used(self):
        with mock.patch.object(cp, "fetch_page", return_value="<html></html>"), \
                mock.patch.object(cp, "find_browser", return_value=""):
            (repo,) = cp.capture([FakeRepo()], self.out)
        self.assertEqual(repo.preview_url, "")

    def test_reuses_an_image_captured_from_the_same_revision(self):
        self.out.mkdir(parents=True)
        (self.out / "proj.png").write_bytes(b"png")
        cp.save_manifest(
            self.out / cp.MANIFEST_NAME,
            {"proj": {"updated_at": "2025-01-01T00:00:00Z", "file": "proj.png",
                      "source": "screenshot"}},
        )
        with mock.patch.object(cp, "fetch_page") as fetch, \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            (repo,) = self._capture([FakeRepo()])
        fetch.assert_not_called()
        self.assertEqual(repo.preview_url, "previews/proj.png")

    def test_recaptures_when_the_repository_changed(self):
        self.out.mkdir(parents=True)
        (self.out / "proj.png").write_bytes(b"png")
        cp.save_manifest(
            self.out / cp.MANIFEST_NAME,
            {"proj": {"updated_at": "2020-01-01T00:00:00Z", "file": "proj.png",
                      "source": "screenshot"}},
        )
        with mock.patch.object(cp, "fetch_page", return_value="<html></html>"), \
                mock.patch.object(cp, "screenshot", return_value=True) as shot, \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            self._capture([FakeRepo()])
        shot.assert_called_once()

    def test_refresh_ignores_the_freshness_check(self):
        self.out.mkdir(parents=True)
        (self.out / "proj.png").write_bytes(b"png")
        cp.save_manifest(
            self.out / cp.MANIFEST_NAME,
            {"proj": {"updated_at": "2025-01-01T00:00:00Z", "file": "proj.png",
                      "source": "screenshot"}},
        )
        with mock.patch.object(cp, "fetch_page", return_value="<html></html>"), \
                mock.patch.object(cp, "screenshot", return_value=True) as shot, \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            self._capture([FakeRepo()], refresh=True)
        shot.assert_called_once()

    def test_writes_manifest_and_prunes_dropped_projects(self):
        self.out.mkdir(parents=True)
        (self.out / "gone.png").write_bytes(b"png")
        cp.save_manifest(
            self.out / cp.MANIFEST_NAME,
            {"gone": {"updated_at": "x", "file": "gone.png", "source": "screenshot"}},
        )
        with mock.patch.object(cp, "fetch_page", return_value="<html></html>"), \
                mock.patch.object(cp, "screenshot", return_value=True), \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            self._capture([FakeRepo()])
        manifest = json.loads((self.out / cp.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(list(manifest), ["proj"])
        self.assertEqual(manifest["proj"]["source"], "screenshot")
        self.assertFalse((self.out / "gone.png").exists())

    def test_url_prefix_overrides_the_directory_name(self):
        with mock.patch.object(cp, "fetch_page", return_value="<html></html>"), \
                mock.patch.object(cp, "screenshot", return_value=True), \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            (repo,) = self._capture([FakeRepo()], url_prefix="assets/shots")
        self.assertEqual(repo.preview_url, "assets/shots/proj.png")

    def test_failed_screenshot_leaves_the_card_without_an_image(self):
        with mock.patch.object(cp, "fetch_page", return_value="<html></html>"), \
                mock.patch.object(cp, "screenshot", return_value=False), \
                mock.patch.object(cp, "find_browser", return_value="/usr/bin/chrome"):
            (repo,) = self._capture([FakeRepo()])
        self.assertEqual(repo.preview_url, "")


if __name__ == "__main__":
    unittest.main()
