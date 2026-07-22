#!/usr/bin/env python3
"""Unit tests for build_index.py.

All network access is mocked; tests exercise the pure formatting logic and the
orchestration path with a fake fetch. Run with: python -m unittest discover -s scripts
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import build_index as bi


def make_raw(**overrides) -> dict:
    base = {
        "name": "repo",
        "description": "desc",
        "homepage": "https://example.com",
        "updated_at": "2025-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


class ParseRepositoriesTest(unittest.TestCase):
    def test_keeps_only_entries_with_homepage(self):
        payload = [
            make_raw(name="a", homepage="https://a.example"),
            make_raw(name="b", homepage=""),
            make_raw(name="c", homepage=None),
            make_raw(name="d", homepage="   "),
        ]
        repos = bi.parse_repositories(payload)
        self.assertEqual([r.name for r in repos], ["a"])

    def test_strips_whitespace_in_fields(self):
        payload = [make_raw(homepage="  https://x.example  ", description="  hi  ")]
        (repo,) = bi.parse_repositories(payload)
        self.assertEqual(repo.homepage, "https://x.example")
        self.assertEqual(repo.description, "hi")

    def test_rejects_non_http_schemes(self):
        payload = [
            make_raw(name="js", homepage="javascript:alert(1)"),
            make_raw(name="ftp", homepage="ftp://x.example"),
            make_raw(name="rel", homepage="//x.example"),
            make_raw(name="bare", homepage="x.example"),
            make_raw(name="ok-http", homepage="http://x.example"),
            make_raw(name="ok-https", homepage="HTTPS://x.example"),
        ]
        repos = bi.parse_repositories(payload)
        self.assertEqual([r.name for r in repos], ["ok-http", "ok-https"])

    def test_missing_optional_fields_default_empty(self):
        payload = [{"name": "n", "homepage": "https://n.example"}]
        (repo,) = bi.parse_repositories(payload)
        self.assertEqual(repo.description, "")
        self.assertEqual(repo.updated_at, "")


class SortRepositoriesTest(unittest.TestCase):
    def test_sorts_newest_first(self):
        repos = [
            bi.Repo("old", "", "u", "2020-01-01T00:00:00Z"),
            bi.Repo("new", "", "u", "2025-01-01T00:00:00Z"),
            bi.Repo("mid", "", "u", "2023-01-01T00:00:00Z"),
        ]
        result = bi.sort_repositories(repos)
        self.assertEqual([r.name for r in result], ["new", "mid", "old"])


class UpdatedDateTest(unittest.TestCase):
    def test_formats_iso_to_date(self):
        repo = bi.Repo("n", "", "u", "2025-03-14T09:26:53Z")
        self.assertEqual(repo.updated_date, "2025-03-14")

    def test_unparsable_returns_empty(self):
        repo = bi.Repo("n", "", "u", "not-a-date")
        self.assertEqual(repo.updated_date, "")


class RenderMarkdownTest(unittest.TestCase):
    HEADER = "| Repository | Description | Updated |\n| --- | --- | --- |"

    def test_renders_entries(self):
        repos = [bi.Repo("proj", "a tool", "https://proj.example", "2025-01-02T00:00:00Z")]
        md = bi.render_markdown(repos)
        self.assertEqual(
            md,
            f"{self.HEADER}\n| [proj](https://proj.example) | a tool | 2025-01-02 |",
        )

    def test_entry_without_description(self):
        repos = [bi.Repo("proj", "", "https://proj.example", "2025-01-02T00:00:00Z")]
        md = bi.render_markdown(repos)
        self.assertEqual(
            md, f"{self.HEADER}\n| [proj](https://proj.example) |  | 2025-01-02 |"
        )

    def test_escapes_pipes_in_cells(self):
        repos = [bi.Repo("a|b", "x | y", "https://p.example", "2025-01-02T00:00:00Z")]
        md = bi.render_markdown(repos)
        self.assertIn(r"| [a\|b](https://p.example) | x \| y | 2025-01-02 |", md)

    def test_empty_list(self):
        self.assertEqual(bi.render_markdown([]), "_No published repositories found._")


class ReplaceReadmeSectionTest(unittest.TestCase):
    def test_replaces_between_markers(self):
        readme = f"# Title\n{bi.README_START}\nold\n{bi.README_END}\ntail"
        result = bi.replace_readme_section(readme, "NEW")
        self.assertEqual(
            result, f"# Title\n{bi.README_START}\nNEW\n{bi.README_END}\ntail"
        )

    def test_is_idempotent(self):
        readme = f"{bi.README_START}\nx\n{bi.README_END}"
        once = bi.replace_readme_section(readme, "SECTION")
        twice = bi.replace_readme_section(once, "SECTION")
        self.assertEqual(once, twice)

    def test_missing_markers_raises(self):
        with self.assertRaises(ValueError):
            bi.replace_readme_section("no markers here", "x")

    def test_reversed_markers_raises(self):
        readme = f"{bi.README_END}\n{bi.README_START}"
        with self.assertRaises(ValueError):
            bi.replace_readme_section(readme, "x")


class RenderHtmlTest(unittest.TestCase):
    def test_escapes_dynamic_values(self):
        repos = [
            bi.Repo(
                name="<b>x</b>",
                description='a & "b"',
                homepage="https://x.example/?a=1&b=2",
                updated_at="2025-01-01T00:00:00Z",
            )
        ]
        out = bi.render_html(repos, "2025-01-01 00:00 UTC")
        self.assertNotIn("<b>x</b>", out)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)
        self.assertIn("a=1&amp;b=2", out)

    def test_empty_list_renders_placeholder(self):
        out = bi.render_html([], "t")
        self.assertIn("No published repositories found.", out)

    def test_includes_generated_timestamp(self):
        out = bi.render_html([], "2025-06-01 12:00 UTC")
        self.assertIn("2025-06-01 12:00 UTC", out)


class BuildOrchestrationTest(unittest.TestCase):
    def test_build_writes_both_outputs(self):
        raw = [
            make_raw(name="new", homepage="https://new.example", updated_at="2025-05-01T00:00:00Z"),
            make_raw(name="old", homepage="https://old.example", updated_at="2020-05-01T00:00:00Z"),
            make_raw(name="skip", homepage=""),
        ]
        with tempfile.TemporaryDirectory() as d:
            readme = Path(d) / "README.md"
            html_out = Path(d) / "docs" / "index.html"
            readme.write_text(
                f"# Me\n{bi.README_START}\nplaceholder\n{bi.README_END}\n",
                encoding="utf-8",
            )
            with mock.patch.object(bi, "fetch_repositories", return_value=raw):
                repos = bi.build(
                    "someone",
                    readme,
                    html_out,
                    now=datetime(2025, 6, 1, tzinfo=timezone.utc),
                )
            self.assertEqual([r.name for r in repos], ["new", "old"])
            readme_text = readme.read_text(encoding="utf-8")
            self.assertIn("[new](https://new.example)", readme_text)
            self.assertNotIn("skip", readme_text)
            # new must appear before old (newest first)
            self.assertLess(
                readme_text.index("new"), readme_text.index("old")
            )
            self.assertTrue(html_out.exists())
            self.assertIn("new.example", html_out.read_text(encoding="utf-8"))

    def test_build_passes_token_to_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            readme = Path(d) / "README.md"
            readme.write_text(f"{bi.README_START}\n{bi.README_END}", encoding="utf-8")
            with mock.patch.object(
                bi, "fetch_repositories", return_value=[]
            ) as fetch:
                bi.build("u", readme, Path(d) / "out.html", token="secret")
            fetch.assert_called_once_with("u", token="secret")


if __name__ == "__main__":
    unittest.main()
