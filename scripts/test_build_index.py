#!/usr/bin/env python3
"""Unit tests for build_index.py.

All network access is mocked; tests exercise the pure formatting logic and the
orchestration path with a fake fetch. Run with: python -m unittest discover -s scripts
"""

from __future__ import annotations

import re
import tempfile
import unittest
import urllib.error
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
        "html_url": "https://github.com/u/repo",
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
        self.assertEqual(repo.html_url, "")

    def test_excludes_forks(self):
        payload = [
            make_raw(name="own", fork=False),
            make_raw(name="forked", fork=True),
            make_raw(name="no-flag"),  # missing key treated as not a fork
        ]
        repos = bi.parse_repositories(payload)
        self.assertEqual([r.name for r in repos], ["own", "no-flag"])

    def test_captures_html_url(self):
        payload = [make_raw(html_url="https://github.com/u/repo")]
        (repo,) = bi.parse_repositories(payload)
        self.assertEqual(repo.html_url, "https://github.com/u/repo")


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
    def test_renders_entries(self):
        repos = [
            bi.Repo(
                "proj",
                "a tool",
                "https://u.github.io/proj/",
                "2025-01-02T00:00:00Z",
                "https://github.com/u/proj",
            )
        ]
        md = bi.render_markdown(repos)
        self.assertEqual(
            md,
            "- **[proj](https://github.com/u/proj)** "
            "· [pages](https://u.github.io/proj/) "
            "· <sub>2025-01-02</sub><br>\n"
            "  a tool",
        )

    def test_entry_without_html_url_renders_plain_name(self):
        repos = [bi.Repo("proj", "", "https://u.github.io/proj/", "2025-01-02T00:00:00Z")]
        md = bi.render_markdown(repos)
        self.assertEqual(
            md,
            "- **proj** · [pages](https://u.github.io/proj/) · <sub>2025-01-02</sub>",
        )

    def test_omits_date_when_unparsable(self):
        repos = [bi.Repo("proj", "", "https://u.github.io/proj/", "not-a-date")]
        md = bi.render_markdown(repos)
        self.assertEqual(md, "- **proj** · [pages](https://u.github.io/proj/)")

    def test_flattens_newlines_in_fields(self):
        repos = [
            bi.Repo(
                "a\nb",
                "line one\nline two",
                "https://p.example",
                "2025-01-02T00:00:00Z",
                "https://github.com/u/ab",
            )
        ]
        md = bi.render_markdown(repos)
        self.assertIn("[a b](https://github.com/u/ab)", md)
        self.assertIn("  line one line two", md)

    def test_description_continues_on_indented_line(self):
        repos = [
            bi.Repo(
                "proj",
                "a tool",
                "https://u.github.io/proj/",
                "2025-01-02T00:00:00Z",
                "https://github.com/u/proj",
            )
        ]
        lines = bi.render_markdown(repos).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("- "))
        self.assertTrue(lines[0].endswith("<br>"))
        self.assertTrue(lines[1].startswith("  "))

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


class IsMediaUrlTest(unittest.TestCase):
    def test_accepts_github_asset_hosts(self):
        self.assertTrue(bi.is_media_url("https://raw.githubusercontent.com/u/r/main/a.gif"))
        self.assertTrue(bi.is_media_url("https://avatars.githubusercontent.com/u/1?v=4"))

    def test_rejects_other_hosts_and_schemes(self):
        for url in (
            "https://evil.example/a.gif",
            "http://raw.githubusercontent.com/u/r/main/a.gif",
            "javascript:alert(1)",
            "https://raw.githubusercontent.com.evil.example/a.gif",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(bi.is_media_url(url))


def make_entry(name: str, url: str = "", type_: str = "file") -> dict:
    return {
        "name": name,
        "type": type_,
        "download_url": url or f"https://raw.githubusercontent.com/u/r/main/{name}",
    }


class SelectPreviewTest(unittest.TestCase):
    def test_picks_named_preview(self):
        entries = [make_entry("index.html"), make_entry("preview.gif"), make_entry("logo.png")]
        self.assertEqual(
            bi.select_preview(entries),
            "https://raw.githubusercontent.com/u/r/main/preview.gif",
        )

    def test_prefers_animated_formats(self):
        entries = [make_entry("preview.png"), make_entry("demo.gif")]
        self.assertTrue(bi.select_preview(entries).endswith("demo.gif"))

    def test_accepts_every_format_a_card_can_show(self):
        for extension in (".gif", ".apng", ".svg", ".webp", ".avif",
                          ".png", ".jpg", ".jpeg", ".bmp"):
            with self.subTest(extension=extension):
                entries = [make_entry(f"preview{extension}")]
                self.assertTrue(bi.select_preview(entries).endswith(extension))

    def test_ranks_what_moves_over_what_does_not(self):
        ranks = bi.PREVIEW_EXTENSIONS
        self.assertLess(ranks[".gif"], ranks[".svg"])
        self.assertLess(ranks[".svg"], ranks[".png"])
        self.assertLess(ranks[".png"], ranks[".bmp"])
        # a project shipping several: the one that can move wins
        entries = [make_entry("preview.bmp"), make_entry("preview.svg"),
                   make_entry("preview.png")]
        self.assertTrue(bi.select_preview(entries).endswith("preview.svg"))

    def test_prefers_earlier_stem_at_equal_format(self):
        entries = [make_entry("screenshot.png"), make_entry("preview.png")]
        self.assertTrue(bi.select_preview(entries).endswith("preview.png"))

    def test_accepts_suffixed_stems(self):
        entries = [make_entry("preview-dark.webp")]
        self.assertTrue(bi.select_preview(entries).endswith("preview-dark.webp"))

    def test_ignores_unrelated_names_and_types(self):
        entries = [
            make_entry("banner", url="https://raw.githubusercontent.com/u/r/main/banner"),
            make_entry("style.css"),
            make_entry("previewer.png"),  # stem must match on a -/_ boundary
            make_entry("preview", type_="dir"),
        ]
        self.assertEqual(bi.select_preview(entries), "")

    def test_rejects_off_domain_download_urls(self):
        entries = [make_entry("preview.gif", url="https://evil.example/preview.gif")]
        self.assertEqual(bi.select_preview(entries), "")

    def test_is_deterministic_regardless_of_listing_order(self):
        a = [make_entry("demo.gif"), make_entry("preview.gif")]
        self.assertEqual(bi.select_preview(a), bi.select_preview(list(reversed(a))))

    def test_empty_listing(self):
        self.assertEqual(bi.select_preview([]), "")


class ParseProfileTest(unittest.TestCase):
    def test_reads_fields(self):
        profile = bi.parse_profile(
            {
                "login": "someone",
                "name": "Some One",
                "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4",
                "bio": "a  b\nc",
            },
            "someone",
        )
        self.assertEqual(profile.display_name, "Some One")
        self.assertEqual(profile.avatar_url, "https://avatars.githubusercontent.com/u/1?v=4")
        self.assertEqual(profile.bio, "a b c")
        self.assertEqual(profile.html_url, "https://github.com/someone")

    def test_drops_off_domain_avatar(self):
        profile = bi.parse_profile({"avatar_url": "https://evil.example/a.png"}, "u")
        self.assertEqual(profile.avatar_url, "")

    def test_falls_back_to_login_for_display_name(self):
        self.assertEqual(bi.parse_profile({}, "someone").display_name, "someone")


class TileTest(unittest.TestCase):
    def test_monogram_uses_leading_alphanumerics(self):
        self.assertEqual(bi._monogram("dowel"), "do")
        self.assertEqual(bi._monogram("-x-y"), "xy")
        self.assertEqual(bi._monogram("---"), "?")

    def test_hue_is_stable_and_in_range(self):
        self.assertEqual(bi._hue("dowel"), bi._hue("dowel"))
        self.assertNotEqual(bi._hue("dowel"), bi._hue("deco"))
        self.assertTrue(0 <= bi._hue("dowel") < 360)


class TagsTest(unittest.TestCase):
    def test_normalize_lowercases_and_removes_spaces(self):
        self.assertEqual(bi.normalize_tag("Jupyter Notebook"), "jupyter-notebook")
        self.assertEqual(bi.normalize_tag("  Rust  "), "rust")

    def test_normalize_keeps_language_punctuation(self):
        self.assertEqual(bi.normalize_tag("C++"), "c++")
        self.assertEqual(bi.normalize_tag("C#"), "c#")

    def test_normalize_drops_other_characters(self):
        self.assertEqual(bi.normalize_tag("a/b<c>"), "abc")
        self.assertEqual(bi.normalize_tag("!!!"), "")

    def test_parse_uses_topics_and_ignores_the_language(self):
        # language detection must not speak over what the author chose to say
        item = make_raw(topics=["fpga", "Tang-Nano-9K"], language="PowerShell")
        self.assertEqual(bi.parse_tags(item), ("fpga", "tang-nano-9k"))

    def test_parse_falls_back_to_the_language_without_topics(self):
        for topics in (None, []):
            with self.subTest(topics=topics):
                item = make_raw(topics=topics, language="MoonBit")
                self.assertEqual(bi.parse_tags(item), ("moonbit",))

    def test_parse_deduplicates_repeated_topics(self):
        item = make_raw(topics=["rust", "Rust", "editor"])
        self.assertEqual(bi.parse_tags(item), ("rust", "editor"))

    def test_parse_tolerates_missing_and_malformed_values(self):
        self.assertEqual(bi.parse_tags({}), ())
        self.assertEqual(bi.parse_tags({"topics": None, "language": None}), ())
        self.assertEqual(bi.parse_tags({"topics": [1, "ok", None]}), ("ok",))
        self.assertEqual(bi.parse_tags({"topics": ["!!!"], "language": "Rust"}), ("rust",))
        self.assertEqual(bi.parse_tags({"language": 42}), ())

    def test_repositories_carry_their_tags(self):
        payload = [make_raw(topics=["fpga"], language="SystemVerilog")]
        (repo,) = bi.parse_repositories(payload)
        self.assertEqual(repo.tags, ("fpga",))

    def test_collect_orders_by_use_then_name(self):
        repos = [
            bi.Repo("a", "", "u", "t", tags=("rust", "cli")),
            bi.Repo("b", "", "u", "t", tags=("rust", "web")),
            bi.Repo("c", "", "u", "t", tags=("rust", "cli")),
        ]
        self.assertEqual(bi.collect_tags(repos), [("rust", 3), ("cli", 2), ("web", 1)])

    def test_collect_is_capped(self):
        repos = [bi.Repo("a", "", "u", "t", tags=tuple(f"t{i}" for i in range(30)))]
        self.assertEqual(len(bi.collect_tags(repos)), bi.MAX_FILTER_TAGS)
        self.assertEqual(len(bi.collect_tags(repos, limit=3)), 3)


class RenderTagBarTest(unittest.TestCase):
    def repos(self):
        return [
            bi.Repo("a", "", "https://a.example", "t", tags=("rust", "cli")),
            bi.Repo("b", "", "https://b.example", "t", tags=("rust",)),
        ]

    def test_bar_lists_every_tag_as_a_pressable_chip(self):
        out = bi.render_html(self.repos(), "t")
        self.assertIn('<nav class="tags" aria-label="filter projects by tag">', out)
        for tag, count in (("rust", 2), ("cli", 1)):
            self.assertIn(
                f'<button type="button" class="tag" data-tag="{tag}"'
                f' aria-pressed="false">{tag}<span class="n">{count}</span></button>',
                out,
            )

    def test_cards_carry_their_tags(self):
        out = bi.render_html(self.repos(), "t")
        self.assertIn('data-tags="rust cli"', out)
        self.assertIn('data-tags="rust"', out)

    def test_bar_is_omitted_when_there_is_nothing_to_filter(self):
        # one tag would only ever hide the rest, and no tags means no bar
        for tags in ((), ("rust",)):
            with self.subTest(tags=tags):
                repos = [bi.Repo("a", "", "https://a.example", "t", tags=tags)]
                self.assertNotIn('class="tags"', bi.render_html(repos, "t"))

    def test_bar_escapes_tag_values(self):
        repos = [
            bi.Repo("a", "", "https://a.example", "t", tags=('x"y', "z")),
            bi.Repo("b", "", "https://b.example", "t", tags=('x"y',)),
        ]
        out = bi.render_html(repos, "t")
        self.assertNotIn('data-tag="x"y"', out)
        self.assertIn("&quot;", out)

    def test_controls_are_inert_without_scripting(self):
        out = bi.render_html(self.repos(), "t")
        self.assertIn('document.documentElement.classList.add("js")', out)
        self.assertIn(":root:not(.js) .tags { display: none; }", out)
        self.assertIn(":root:not(.js) .card-tags .tag {", out)

    def test_hidden_cards_take_display_back_from_the_class_rule(self):
        # .card sets display, which outranks the UA rule for [hidden]
        self.assertIn(".card[hidden] { display: none; }", bi.render_html(self.repos(), "t"))

    def test_cards_show_their_own_tags_as_the_same_control(self):
        out = bi.render_html(self.repos(), "t")
        card = out.split('<article class="card"')[1]
        self.assertIn('<p class="card-tags">', card)
        self.assertIn(
            '<button type="button" class="tag" data-tag="rust" aria-pressed="false">'
            "rust</button>",
            card,
        )

    def test_card_tags_omit_the_count(self):
        card = bi.render_html(self.repos(), "t").split('<article class="card"')[1]
        self.assertNotIn('class="n"', card.split("</article>")[0])

    def test_card_tag_list_is_capped(self):
        many = tuple(f"t{i}" for i in range(bi.MAX_CARD_TAGS + 4))
        repos = [
            bi.Repo("a", "", "https://a.example", "t", tags=many),
            bi.Repo("b", "", "https://b.example", "t", tags=many),
        ]
        card = bi.render_html(repos, "t").split('<article class="card"')[1]
        card = card.split("</article>")[0]
        self.assertEqual(card.count('<button type="button"'), bi.MAX_CARD_TAGS)

    def test_cards_without_tags_get_no_tag_row(self):
        repos = [
            bi.Repo("a", "", "https://a.example", "t", tags=("rust", "cli")),
            bi.Repo("b", "", "https://b.example", "t"),
        ]
        second = bi.render_html(repos, "t").split('<article class="card"')[2]
        self.assertNotIn('class="card-tags"', second)

    def test_count_is_addressable_for_the_filter_script(self):
        out = bi.render_html(self.repos(), "t")
        self.assertIn('works (<span class="count">2</span>)', out)
        self.assertIn('document.querySelector(".section .count")', out)


class AccentTest(unittest.TestCase):
    """The accent hue is rolled per visit; both themes must follow the same hue."""

    def setUp(self):
        self.page = bi.render_html([], "t")

    def test_rolls_a_hue_on_load(self):
        self.assertIn('setProperty("--ha", Math.floor(Math.random() * 360))', self.page)

    def test_both_themes_read_the_rolled_hue(self):
        accents = re.findall(r"--accent: oklch\(([\d.]+) ([\d.]+) var\(--ha, \d+\)\);",
                             self.page)
        self.assertEqual(len(accents), 2)
        dark, light = accents
        # the dark theme's accent is the lighter of the two, and neither is
        # near-white or near-black, which would vanish into a background
        self.assertGreater(float(dark[0]), float(light[0]))
        for lightness, chroma in accents:
            self.assertTrue(0.3 < float(lightness) < 0.9)
            self.assertGreater(float(chroma), 0.1)

    def test_keeps_a_hex_fallback_before_each_oklch(self):
        for hex_fallback in ("--accent: #6cb6ff;", "--accent: #0a58b8;"):
            self.assertIn(hex_fallback, self.page)


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

    def test_renders_card_with_live_and_source_links(self):
        repos = [
            bi.Repo(
                "proj",
                "a tool",
                "https://u.github.io/proj/",
                "2025-01-01T00:00:00Z",
                "https://github.com/u/proj",
            )
        ]
        out = bi.render_html(repos, "t")
        self.assertIn('<article class="card" style="--i: 0" data-tags="">', out)
        self.assertIn('<h3><a href="https://u.github.io/proj/">proj</a></h3>', out)
        self.assertIn('<a href="https://github.com/u/proj">source</a>', out)
        self.assertIn('<p class="desc">a tool</p>', out)
        self.assertIn('<span class="date">2025-01-01</span>', out)

    def test_renders_preview_image_when_available(self):
        repos = [
            bi.Repo(
                "proj",
                "",
                "https://u.github.io/proj/",
                "2025-01-01T00:00:00Z",
                preview_url="https://raw.githubusercontent.com/u/proj/main/docs/preview.gif",
            )
        ]
        out = bi.render_html(repos, "t")
        self.assertIn(
            '<img src="https://raw.githubusercontent.com/u/proj/main/docs/preview.gif" '
            'alt="preview of proj" loading="lazy" decoding="async">',
            out,
        )
        self.assertNotIn('class="tile"', out)

    def test_dark_preview_is_offered_alongside_the_light_one(self):
        repos = [
            bi.Repo(
                "proj",
                "",
                "https://u.github.io/proj/",
                "2025-01-01T00:00:00Z",
                preview_url="previews/proj.png",
                preview_dark_url="previews/proj-dark.png",
            )
        ]
        out = bi.render_html(repos, "t")
        self.assertIn(
            '<picture><source srcset="previews/proj-dark.png"'
            ' media="(prefers-color-scheme: dark)">'
            '<img src="previews/proj.png" alt="preview of proj"'
            ' loading="lazy" decoding="async"></picture>',
            out,
        )

    def test_no_picture_element_without_a_dark_capture(self):
        repos = [
            bi.Repo("proj", "", "https://u.github.io/proj/", "t",
                    preview_url="previews/proj.png")
        ]
        out = bi.render_html(repos, "t")
        self.assertNotIn("<picture>", out)
        self.assertIn('<img src="previews/proj.png"', out)

    def test_falls_back_to_monogram_tile(self):
        repos = [bi.Repo("proj", "", "https://u.github.io/proj/", "2025-01-01T00:00:00Z")]
        out = bi.render_html(repos, "t")
        self.assertIn(f'<span class="tile" style="--h: {bi._hue("proj")}"', out)
        self.assertIn("<span>pr</span>", out)
        self.assertNotIn("<img src=", out)

    def test_omits_source_link_without_html_url(self):
        repos = [bi.Repo("proj", "", "https://u.github.io/proj/", "2025-01-01T00:00:00Z")]
        self.assertNotIn(">source</a>", bi.render_html(repos, "t"))

    def test_renders_owner_heading_and_profile_link(self):
        out = bi.render_html([], "t", owner="someone")
        self.assertIn("<h1>someone</h1>", out)
        self.assertIn('<a href="https://github.com/someone">github.com/someone</a>', out)

    def test_profile_supplies_avatar_and_display_name(self):
        profile = bi.Profile(
            login="someone",
            name="Some One",
            avatar_url="https://avatars.githubusercontent.com/u/1?v=4",
        )
        out = bi.render_html([], "t", owner="someone", profile=profile)
        self.assertIn("<h1>Some One</h1>", out)
        self.assertIn(
            '<img class="avatar" src="https://avatars.githubusercontent.com/u/1?v=4"', out
        )

    def test_omits_avatar_when_profile_has_none(self):
        out = bi.render_html([], "t", owner="u", profile=bi.Profile(login="u"))
        self.assertNotIn('class="avatar"', out)
        self.assertIn("<h1>u</h1>", out)

    def test_renders_tagline_when_given(self):
        out = bi.render_html([], "t", owner="u", tagline="hello <world>")
        self.assertIn('<p class="tagline">hello &lt;world&gt;</p>', out)

    def test_tagline_falls_back_to_bio(self):
        profile = bi.Profile(login="u", bio="mochimochi engineer.")
        out = bi.render_html([], "t", owner="u", profile=profile)
        self.assertIn('<p class="tagline">mochimochi engineer.</p>', out)

    def test_explicit_tagline_wins_over_bio(self):
        profile = bi.Profile(login="u", bio="from bio")
        out = bi.render_html([], "t", owner="u", tagline="explicit", profile=profile)
        self.assertIn('<p class="tagline">explicit</p>', out)
        self.assertNotIn("from bio", out)

    def test_omits_tagline_and_profile_when_absent(self):
        out = bi.render_html([], "t")
        self.assertNotIn('class="tagline"', out)
        self.assertNotIn('class="profile"', out)
        self.assertIn("<h1>Published Repositories</h1>", out)

    def test_cards_carry_their_position_for_the_entrance_delay(self):
        repos = [
            bi.Repo(n, "", f"https://u.github.io/{n}/", "2025-01-01T00:00:00Z")
            for n in ("a", "b", "c")
        ]
        out = bi.render_html(repos, "t")
        positions = re.findall(r'<article class="card" style="--i: (\d+)"', out)
        self.assertEqual(positions, ["0", "1", "2"])

    def test_section_label_counts_projects(self):
        repos = [bi.Repo("proj", "", "https://u.github.io/proj/", "2025-01-01T00:00:00Z")]
        self.assertIn('works (<span class="count">1</span>)', bi.render_html(repos, "t"))

    def test_empty_list_renders_placeholder(self):
        out = bi.render_html([], "t")
        self.assertIn("No published repositories found.", out)

    def test_includes_generated_timestamp(self):
        out = bi.render_html([], "2025-06-01 12:00 UTC")
        self.assertIn("2025-06-01 12:00 UTC", out)

    def test_renders_copyright_for_the_owner(self):
        out = bi.render_html([], "t", owner="someone", year="2026")
        self.assertIn('<p class="copyright">© 2026 someone</p>', out)

    def test_copyright_names_the_display_name(self):
        profile = bi.Profile(login="someone", name="Some One")
        out = bi.render_html([], "t", owner="someone", year="2026", profile=profile)
        self.assertIn('<p class="copyright">© 2026 Some One</p>', out)

    def test_omits_copyright_without_a_year(self):
        self.assertNotIn('class="copyright"', bi.render_html([], "t", owner="someone"))


class FetchPreviewTest(unittest.TestCase):
    def test_returns_first_directory_that_yields_a_match(self):
        listings = {"docs": [], "docs/assets": [make_entry("preview.gif")]}

        def fake_get(url, token=None):
            path = url.split("/contents/", 1)[1]
            if path not in listings:
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            return listings[path]

        with mock.patch.object(bi, "_get_json", side_effect=fake_get) as get:
            url = bi.fetch_preview("u", "r")
        self.assertTrue(url.endswith("preview.gif"))
        # stopped as soon as a preview was found
        self.assertEqual(len(get.call_args_list), 2)

    def test_missing_directories_yield_no_preview(self):
        def fake_get(url, token=None):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        with mock.patch.object(bi, "_get_json", side_effect=fake_get):
            self.assertEqual(bi.fetch_preview("u", "r"), "")

    def test_network_error_is_not_fatal(self):
        with mock.patch.object(
            bi, "_get_json", side_effect=urllib.error.URLError("down")
        ):
            self.assertEqual(bi.fetch_preview("u", "r"), "")

    def test_file_response_is_skipped(self):
        with mock.patch.object(bi, "_get_json", return_value={"type": "file"}):
            self.assertEqual(bi.fetch_preview("u", "r"), "")


class FetchProfileTest(unittest.TestCase):
    def test_parses_payload(self):
        payload = {"login": "u", "name": "U", "avatar_url": "https://avatars.githubusercontent.com/u/1"}
        with mock.patch.object(bi, "_get_json", return_value=payload):
            self.assertEqual(bi.fetch_profile("u").display_name, "U")

    def test_network_error_degrades_to_login_only(self):
        with mock.patch.object(
            bi, "_get_json", side_effect=urllib.error.URLError("down")
        ):
            profile = bi.fetch_profile("u")
        self.assertEqual(profile, bi.Profile(login="u"))


class BuildOrchestrationTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(bi, "fetch_preview", return_value=""),
            mock.patch.object(bi, "fetch_profile", return_value=bi.Profile(login="someone")),
            mock.patch.object(bi.capture_previews, "capture", side_effect=lambda r, *a, **k: list(r)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

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
            self.assertIn("(https://new.example)", readme_text)
            self.assertNotIn("skip", readme_text)
            # new must appear before old (newest first)
            self.assertLess(
                readme_text.index("new"), readme_text.index("old")
            )
            self.assertTrue(html_out.exists())
            page = html_out.read_text(encoding="utf-8")
            self.assertIn("new.example", page)
            # the copyright year comes from the build timestamp
            self.assertIn("© 2025 someone", page)

    def test_build_passes_token_to_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            readme = Path(d) / "README.md"
            readme.write_text(f"{bi.README_START}\n{bi.README_END}", encoding="utf-8")
            with mock.patch.object(
                bi, "fetch_repositories", return_value=[]
            ) as fetch:
                bi.build("u", readme, Path(d) / "out.html", token="secret")
            fetch.assert_called_once_with("u", token="secret")

    def test_build_attaches_previews(self):
        raw = [make_raw(name="proj", homepage="https://p.example")]
        with tempfile.TemporaryDirectory() as d:
            readme = Path(d) / "README.md"
            readme.write_text(f"{bi.README_START}\n{bi.README_END}", encoding="utf-8")
            preview = "https://raw.githubusercontent.com/u/proj/main/docs/preview.gif"
            with mock.patch.object(bi, "fetch_repositories", return_value=raw), \
                    mock.patch.object(bi, "fetch_preview", return_value=preview) as fetch:
                (repo,) = bi.build("u", readme, Path(d) / "out.html")
            fetch.assert_called_once_with("u", "proj", token=None)
            self.assertEqual(repo.preview_url, preview)

    def test_build_can_skip_previews(self):
        raw = [make_raw(name="proj", homepage="https://p.example")]
        with tempfile.TemporaryDirectory() as d:
            readme = Path(d) / "README.md"
            readme.write_text(f"{bi.README_START}\n{bi.README_END}", encoding="utf-8")
            with mock.patch.object(bi, "fetch_repositories", return_value=raw), \
                    mock.patch.object(bi, "fetch_preview") as fetch:
                bi.build("u", readme, Path(d) / "out.html", previews=False)
            fetch.assert_not_called()

    def test_build_captures_into_previews_dir_beside_the_page(self):
        raw = [make_raw(name="proj", homepage="https://p.example")]
        with tempfile.TemporaryDirectory() as d:
            readme = Path(d) / "README.md"
            readme.write_text(f"{bi.README_START}\n{bi.README_END}", encoding="utf-8")
            html_out = Path(d) / "docs" / "index.html"
            with mock.patch.object(bi, "fetch_repositories", return_value=raw), \
                    mock.patch.object(bi.capture_previews, "capture") as capture:
                capture.side_effect = lambda r, *a, **k: list(r)
                bi.build("u", readme, html_out)
            args, kwargs = capture.call_args
            self.assertEqual(args[1], Path(d) / "docs" / "previews")
            self.assertEqual(kwargs["url_prefix"], "previews")

    def test_build_can_skip_capture(self):
        with tempfile.TemporaryDirectory() as d:
            readme = Path(d) / "README.md"
            readme.write_text(f"{bi.README_START}\n{bi.README_END}", encoding="utf-8")
            with mock.patch.object(bi, "fetch_repositories", return_value=[]), \
                    mock.patch.object(bi.capture_previews, "capture") as capture:
                bi.build("u", readme, Path(d) / "out.html", capture=False)
            capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
