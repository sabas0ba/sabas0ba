#!/usr/bin/env python3
"""Generate a portfolio of public repositories that publish a homepage (GitHub Pages).

The script collects public repositories for a given GitHub user via the REST API,
keeps only those whose ``homepage`` field holds a non-empty URL, sorts them by
last update (newest first), and renders the result into two outputs:

- a marker-delimited section inside ``README.md`` (for the profile page)
- a static ``docs/index.html`` portfolio page (for GitHub Pages), with the
  owner's avatar, a short profile line, and one card per project

A card's image is, in order of preference: a preview file shipped by the
project repository (``docs/preview.gif`` and friends), an image captured from
the project's published page by ``capture_previews``, or a monogram tile drawn
by the page itself.

Only the Python standard library is used. Network access is confined to the
``fetch_*`` helpers; all formatting logic is pure and unit-testable.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import posixpath
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Iterable, Optional, Sequence

import capture_previews

API_ROOT = "https://api.github.com"
USER_AGENT = "sabas0ba-index-builder"
PER_PAGE = 100
MAX_PAGES = 20  # hard cap: 20 * 100 = 2000 repos; prevents unbounded paging

README_START = "<!-- INDEX:START -->"
README_END = "<!-- INDEX:END -->"

# Where images captured from the projects' own pages are stored, relative to
# the generated HTML page.
DEFAULT_PREVIEW_DIR = "previews"

# How many tags the filter bar offers. Beyond roughly this many the bar stops
# reading as a set of controls and starts reading as a paragraph.
MAX_FILTER_TAGS = 12

# How many of its own tags a card shows. A project with a long topic list would
# otherwise push its own description out of view.
MAX_CARD_TAGS = 6

# Directories probed (in order) for a project preview, and the file stems that
# count as one. The first directory that yields a usable image wins, so a
# repository can override a generic asset by putting one under docs/.
PREVIEW_DIRS = ("docs", "docs/assets", "assets", ".github", "")
PREVIEW_STEMS = ("preview", "demo", "screenshot", "hero", "banner")

# Lower rank = preferred. Animated formats come first: a moving demo says more
# about a web app than a still frame does.
PREVIEW_EXTENSIONS = {
    ".gif": 0,
    ".apng": 1,
    ".webp": 2,  # may be animated; still frames are fine too
    ".avif": 3,
    ".png": 4,
    ".jpg": 5,
    ".jpeg": 5,
}

# Image hosts the generated page is allowed to reference. Everything the API
# hands back for repository contents and avatars lives under this domain;
# anything else is third-party content and gets dropped.
MEDIA_HOST_SUFFIX = ".githubusercontent.com"


@dataclass(frozen=True)
class Repo:
    """Minimal repository metadata used to build the index."""

    name: str
    description: str
    homepage: str
    updated_at: str  # ISO 8601 string as returned by the API
    html_url: str = ""  # repository page on github.com
    preview_url: str = ""  # image/animation shown on the portfolio card
    tags: tuple[str, ...] = ()  # topics and language, for the filter bar

    @property
    def updated_date(self) -> str:
        """Return the update timestamp as a YYYY-MM-DD string, or '' if unparsable."""
        try:
            dt = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return ""
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class Profile:
    """The account the portfolio belongs to."""

    login: str
    name: str = ""
    avatar_url: str = ""
    bio: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.login

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.login}" if self.login else ""


# --------------------------------------------------------------------------- #
# Pure logic (no network) — fully unit-testable
# --------------------------------------------------------------------------- #

def parse_repositories(payload: Sequence[dict]) -> list[Repo]:
    """Convert a raw API payload into Repo objects, keeping only those with a homepage.

    A repository is included when its ``homepage`` field is a non-empty string
    after stripping whitespace and uses an http(s) URL. Other schemes (e.g.
    ``javascript:``) are rejected because the value is emitted as a link href.

    Forks are excluded: their description/homepage are inherited from the
    upstream repository, i.e. third-party-controlled text, which would let an
    upstream author inject content into the generated index. Archived status
    is not considered.
    """
    repos: list[Repo] = []
    for item in payload:
        if item.get("fork"):
            continue
        homepage = (item.get("homepage") or "").strip()
        if not homepage:
            continue
        if urllib.parse.urlparse(homepage).scheme.lower() not in ("http", "https"):
            continue
        repos.append(
            Repo(
                name=item.get("name", ""),
                description=(item.get("description") or "").strip(),
                homepage=homepage,
                updated_at=item.get("updated_at", ""),
                html_url=(item.get("html_url") or "").strip(),
                tags=parse_tags(item),
            )
        )
    return repos


def normalize_tag(value: str) -> str:
    """Reduce a topic or language name to one lowercase, space-free token.

    Tags become whitespace-separated values of a data attribute and are matched
    as whole tokens, so a tag may not contain a space: "Jupyter Notebook"
    becomes "jupyter-notebook". Characters that are neither alphanumeric nor
    part of a language's own name (``c++``, ``c#``, ``f*``) are dropped.
    """
    token = "-".join(value.lower().split())
    return "".join(c for c in token if c.isalnum() or c in "+#*._-").strip("-")


def parse_tags(item: dict) -> tuple[str, ...]:
    """Collect one repository's tags: its GitHub topics, plus its main language.

    The language is included because it is the filter a visitor is most likely
    to reach for, and because a repository with no topics set would otherwise
    be unreachable from the filter bar. Order is preserved and duplicates are
    dropped, so a language that is also a topic appears once.
    """
    raw = list(item.get("topics") or [])
    language = item.get("language")
    if language:
        raw.append(language)
    tags: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        tag = normalize_tag(value)
        if tag and tag not in tags:
            tags.append(tag)
    return tuple(tags)


def collect_tags(
    repos: Sequence[Repo], limit: int = MAX_FILTER_TAGS
) -> list[tuple[str, int]]:
    """Return the tags worth offering as filters, with how many projects carry each.

    Ordering is by that count, descending, then alphabetically so the bar is
    stable between builds. The list is capped: past a dozen or so the bar stops
    being a control and becomes a wall of words.
    """
    counts: dict[str, int] = {}
    for repo in repos:
        for tag in repo.tags:
            counts[tag] = counts.get(tag, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


def sort_repositories(repos: Iterable[Repo]) -> list[Repo]:
    """Return repositories sorted by ``updated_at`` descending (newest first)."""
    return sorted(repos, key=lambda r: r.updated_at, reverse=True)


def is_media_url(url: str) -> bool:
    """Return True for https URLs served by GitHub's own asset domain.

    The generated page embeds these as <img> sources, so anything outside
    GitHub's CDN — or any non-https scheme — is refused rather than trusted.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host.endswith(MEDIA_HOST_SUFFIX)


def select_preview(entries: Sequence[dict]) -> str:
    """Pick the best preview image from one directory listing of the contents API.

    Files are matched by stem (``preview``, ``demo``, … optionally followed by
    ``-``/``_`` and a suffix such as ``preview-dark.gif``) and ranked by format,
    animated first. Returns the download URL, or '' when nothing matches.
    """
    best: Optional[tuple[tuple, str]] = None
    for entry in entries:
        if entry.get("type") != "file":
            continue
        filename = (entry.get("name") or "").strip()
        stem, dot, extension = filename.rpartition(".")
        if not dot:
            continue
        rank = PREVIEW_EXTENSIONS.get("." + extension.lower())
        if rank is None:
            continue
        stem = stem.lower()
        stem_rank = next(
            (
                i
                for i, candidate in enumerate(PREVIEW_STEMS)
                if stem == candidate or stem.startswith((candidate + "-", candidate + "_"))
            ),
            None,
        )
        if stem_rank is None:
            continue
        url = (entry.get("download_url") or "").strip()
        if not is_media_url(url):
            continue
        key = (rank, stem_rank, filename.lower())
        if best is None or key < best[0]:
            best = (key, url)
    return best[1] if best else ""


def parse_profile(payload: dict, login: str) -> Profile:
    """Build a Profile from the ``/users/{login}`` payload, tolerating gaps."""
    avatar = (payload.get("avatar_url") or "").strip()
    return Profile(
        login=(payload.get("login") or login or "").strip(),
        name=(payload.get("name") or "").strip(),
        avatar_url=avatar if is_media_url(avatar) else "",
        bio=" ".join((payload.get("bio") or "").split()),
    )


def _md_text(text: str) -> str:
    """Collapse whitespace so a value cannot break the list-item structure."""
    return " ".join(text.split())


def render_markdown(repos: Sequence[Repo]) -> str:
    """Render the repository list as a Markdown bullet-list fragment.

    A bullet list reflows gracefully at narrow viewport widths, where a table
    would squeeze every column and grow very tall. Each entry links the
    repository name to github.com and the published homepage as a short
    "pages" link; the description, when present, continues on its own line.
    The fragment does not include the surrounding markers; see
    ``replace_readme_section`` for insertion.
    """
    if not repos:
        return "_No published repositories found._"
    lines: list[str] = []
    for r in repos:
        name = _md_text(r.name)
        head = f"**[{name}]({r.html_url})**" if r.html_url else f"**{name}**"
        head += f" · [pages]({r.homepage})"
        if r.updated_date:
            head += f" · <sub>{r.updated_date}</sub>"
        if r.description:
            lines.append(f"- {head}<br>")
            lines.append(f"  {_md_text(r.description)}")
        else:
            lines.append(f"- {head}")
    return "\n".join(lines)


def replace_readme_section(readme: str, section: str) -> str:
    """Replace the content between the INDEX markers with ``section``.

    Raises ValueError if the markers are missing or malformed. The markers
    themselves are preserved so the operation is idempotent.
    """
    start = readme.find(README_START)
    end = readme.find(README_END)
    if start == -1 or end == -1 or end < start:
        raise ValueError(
            f"README markers not found or malformed: "
            f"expected '{README_START}' ... '{README_END}'"
        )
    before = readme[: start + len(README_START)]
    after = readme[end:]
    return f"{before}\n{section}\n{after}"


# No external CSS, JS or fonts are referenced: the page stays self-contained so
# publishing it adds no third-party dependencies. The only remote resources are
# images (avatar, project previews), and those are restricted to GitHub's own
# asset domain by ``is_media_url``. Projects without a preview file fall back to
# a locally drawn monogram tile, so the grid never depends on a remote image.
PAGE_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>$title</title>
  <style>
    :root {
      --bg: #0d1117;
      --panel: #151b23;
      --panel-hi: #1c242e;
      --text: #d5dce3;
      --muted: #7d8894;
      /* The accent hue is rolled on every visit (see the script below); only
         its hue moves. Lightness and chroma are pinned in oklch, which is
         perceptually uniform, so every hue lands at the same apparent
         brightness — measured, the worst hue still clears 8:1 against this
         background and the card panel. The hex above it is what browsers
         without oklch keep, and the fallback hue applies without scripting. */
      --accent: #6cb6ff;
      --accent: oklch(0.8 0.14 var(--ha, 250));
      --line: #262e38;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.5);
      --dot: rgba(220, 235, 255, 0.05);
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #fbfcfd;
        --panel: #ffffff;
        --panel-hi: #f2f5f8;
        --text: #22282f;
        --muted: #5c6672;
        /* Same hue, taken darker: on white, worst case still clears 5:1. */
        --accent: #0a58b8;
        --accent: oklch(0.48 0.16 var(--ha, 250));
        --line: #dde3ea;
        --shadow: 0 1px 2px rgba(16, 22, 30, 0.08);
        --dot: rgba(16, 22, 30, 0.07);
      }
    }
    * { box-sizing: border-box; }
    body {
      font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
      font-size: 0.92rem;
      max-width: 62rem;
      margin: 0 auto;
      padding: 3rem 1.25rem 4rem;
      line-height: 1.65;
      color: var(--text);
      /* Graph-paper dots behind everything: texture at a glance, invisible
         once you start reading. Fixed, so scrolling slides the page over it. */
      background:
        radial-gradient(circle at 1px 1px, var(--dot) 1px, transparent 0)
          0 0 / 26px 26px fixed,
        var(--bg);
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }

    .intro { display: flex; align-items: center; gap: 1.25rem; flex-wrap: wrap; }
    .avatar {
      width: 5.5rem;
      height: 5.5rem;
      border-radius: 50%;
      background: var(--panel-hi);
      border: 1px solid var(--line);
      object-fit: cover;
      flex: none;
    }
    h1 { margin: 0; font-size: 1.35rem; font-weight: 600; letter-spacing: -0.01em; }
    /* A terminal caret parked after the name. Drawn as a box rather than a
       glyph so it does not depend on the font that ends up being used. */
    h1::after {
      content: "";
      display: inline-block;
      width: 0.5em;
      height: 1.05em;
      margin-left: 0.32rem;
      vertical-align: -0.16em;
      background: var(--accent);
      animation: caret 1.15s steps(1, end) infinite;
    }
    @keyframes caret { 0%, 55% { opacity: 1; } 55.01%, 100% { opacity: 0; } }
    .tagline { margin: 0.15rem 0 0; color: var(--text); }
    .profile { margin: 0.15rem 0 0; color: var(--muted); font-size: 0.85rem; }

    /* Sits in the gap between the profile and the works, as its own band. */
    .tags {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      margin: 2.25rem 0 0;
    }
    /* Filtering needs a script. Without one the bar is not shown at all, and a
       card's tags stay plain labels rather than buttons that do nothing. The
       class is set in the head, before the first paint. */
    :root:not(.js) .tags { display: none; }
    :root:not(.js) .card-tags .tag {
      pointer-events: none;
      cursor: default;
      background: none;
      border-color: transparent;
      padding-inline: 0;
    }
    .tag {
      font: inherit;
      font-size: 0.78rem;
      line-height: 1.5;
      color: var(--muted);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0.15rem 0.7rem;
      cursor: pointer;
      transition: color 0.15s ease, border-color 0.15s ease;
    }
    /* The count rides along inside the button, so pressing it still counts
       as pressing the chip. */
    .tag .n { margin-left: 0.4rem; opacity: 0.6; }
    .tag:hover { color: var(--text); border-color: var(--accent); }
    .tag[aria-pressed="true"] {
      color: var(--bg);
      background: var(--accent);
      border-color: var(--accent);
    }

    .section {
      display: flex;
      align-items: baseline;
      gap: 0.6rem;
      margin: 1.6rem 0 1rem;
      font-size: 0.8rem;
      font-weight: 500;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .section::after {
      content: "";
      flex: 1;
      height: 1px;
      background: var(--line);
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr));
      gap: 1.1rem;
    }
    .card {
      display: flex;
      flex-direction: column;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 0.6rem;
      overflow: hidden;
      box-shadow: var(--shadow);
      transition: border-color 0.15s ease, transform 0.15s ease;
      /* Cards deal themselves in on load, one after the next. The delay is
         capped so a long list does not keep the reader waiting. */
      animation: deal 0.45s cubic-bezier(0.2, 0.7, 0.3, 1) backwards;
      animation-delay: calc(min(var(--i), 11) * 55ms);
    }
    @keyframes deal {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: none; }
    }
    /* .card sets display, which would otherwise win over [hidden]'s none. */
    .card[hidden] { display: none; }
    .card:hover { border-color: var(--accent); transform: translateY(-2px); }
    /* The preview leans in a little under the cursor; the card clips it. */
    .card:hover .shot img, .card:hover .tile { transform: scale(1.035); }
    @media (prefers-reduced-motion: reduce) {
      .card, .card:hover { transition: none; transform: none; animation: none; }
      .card:hover .shot img, .card:hover .tile { transform: none; }
      h1::after { animation: none; }
    }

    .shot { display: block; background: var(--panel-hi); overflow: hidden; }
    .shot img {
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      border-bottom: 1px solid var(--line);
      transition: transform 0.3s ease;
    }
    .tile {
      display: flex;
      align-items: center;
      justify-content: center;
      aspect-ratio: 16 / 9;
      border-bottom: 1px solid var(--line);
      transition: transform 0.3s ease;
      background:
        repeating-linear-gradient(
          -45deg,
          rgba(127, 127, 127, 0.08) 0 1px,
          transparent 1px 9px
        ),
        linear-gradient(140deg,
          hsl(var(--h) 52% 30%),
          hsl(calc(var(--h) + 38) 46% 17%));
      color: #f2f6fa;
    }
    @media (prefers-color-scheme: light) {
      .tile {
        background:
          repeating-linear-gradient(
            -45deg,
            rgba(255, 255, 255, 0.35) 0 1px,
            transparent 1px 9px
          ),
          linear-gradient(140deg,
            hsl(var(--h) 62% 78%),
            hsl(calc(var(--h) + 38) 52% 62%));
        color: #16202b;
      }
    }
    .tile span { font-size: 1.9rem; font-weight: 600; letter-spacing: -0.03em; opacity: 0.9; }

    .body { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.9rem 1rem 1rem; flex: 1; }
    .body h3 { margin: 0; font-size: 0.98rem; font-weight: 600; }
    .desc { margin: 0; color: var(--muted); font-size: 0.85rem; line-height: 1.55; }
    .card-tags { display: flex; flex-wrap: wrap; gap: 0.3rem; margin: 0.15rem 0 0; }
    .card-tags .tag { font-size: 0.72rem; padding: 0.02rem 0.5rem; }
    .meta {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin: auto 0 0;  /* auto top margin keeps footers flush across a row */
      padding-top: 0.6rem;
      border-top: 1px solid var(--line);
      font-size: 0.78rem;
      color: var(--muted);
    }
    .meta .sep { opacity: 0.55; }
    .meta .date { margin-left: auto; white-space: nowrap; }

    footer { margin-top: 2.5rem; color: var(--muted); font-size: 0.78rem; }
    footer p { margin: 0; }
    footer .built::before { content: "// "; }
  </style>
  <!-- Runs before the first paint, so neither the colour nor the filter bar
       is seen changing: roll this visit's accent hue, and mark the document
       as scripted so the tag controls are shown at all. With scripting off,
       the stylesheet's fallback hue stands and the controls stay out. -->
  <script>
    document.documentElement.classList.add("js");
    document.documentElement.style.setProperty("--ha", Math.floor(Math.random() * 360));
  </script>
</head>
<body>
  <header class="intro">
$avatar_html    <div>
      <h1>$heading</h1>
$tagline_html$profile_html    </div>
  </header>
$tags_html  <main>
    <h2 class="section">$section_label</h2>
    <div class="grid">
$body
    </div>
  </main>
  <footer>
$copyright_html    <p class="built">generated at $generated_at</p>
  </footer>
  <script>
    // Filter the grid by tag. One listener on the document covers both the bar
    // and the tags on each card, so pressing a tag anywhere does the same
    // thing, and every copy of that tag shows itself pressed.
    (function () {
      var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
      var count = document.querySelector(".section .count");
      var active = "";
      document.addEventListener("click", function (event) {
        var chip = event.target.closest(".tag");
        if (!chip) return;
        active = chip.dataset.tag === active ? "" : chip.dataset.tag;
        document.querySelectorAll(".tag").forEach(function (each) {
          each.setAttribute("aria-pressed", String(each.dataset.tag === active));
        });
        var shown = 0;
        cards.forEach(function (card) {
          var tags = " " + card.dataset.tags + " ";
          var match = !active || tags.indexOf(" " + active + " ") !== -1;
          card.hidden = !match;
          shown += match ? 1 : 0;
        });
        if (count) count.textContent = shown;
      });
    })();
  </script>
</body>
</html>
""")

EMPTY_STATE = (
    '      <p class="desc">No published repositories found.</p>'
)


def _monogram(name: str) -> str:
    """Return up to two leading alphanumeric characters of a project name."""
    letters = [c for c in name.lower() if c.isalnum()]
    return "".join(letters[:2]) or "?"


def _hue(name: str) -> int:
    """Map a project name to a stable hue, so a card keeps its colour over time."""
    return zlib.crc32(name.encode("utf-8")) % 360


def _render_thumb(repo: Repo) -> str:
    """Render the card's visual: the repository's preview media, or a monogram tile."""
    name = html.escape(repo.name)
    if repo.preview_url:
        return (
            f'<img src="{html.escape(repo.preview_url, quote=True)}" '
            f'alt="preview of {name}" loading="lazy" decoding="async">'
        )
    return (
        f'<span class="tile" style="--h: {_hue(repo.name)}" aria-hidden="true">'
        f"<span>{html.escape(_monogram(repo.name))}</span></span>"
    )


def _render_tags(tags: Sequence[tuple[str, int]]) -> str:
    """Render the filter bar, or '' when there is nothing to filter by.

    Each chip carries how many projects it selects, so the size of a filter is
    visible before pressing it. The bar is emitted hidden; the page's script
    reveals it. One tag alone is not a filter — it would only ever hide the
    rest — so it takes at least two for the bar to appear.
    """
    if len(tags) < 2:
        return ""
    chips = "\n".join(
        "      " + _chip(tag, count) for tag, count in tags
    )
    return (
        '  <nav class="tags" aria-label="filter projects by tag">\n'
        f"{chips}\n"
        "  </nav>\n"
    )


def _chip(tag: str, count: Optional[int] = None) -> str:
    """Render one tag as a filter control, optionally with the number it selects."""
    number = f'<span class="n">{count}</span>' if count is not None else ""
    return (
        f'<button type="button" class="tag" data-tag="{html.escape(tag, quote=True)}"'
        f' aria-pressed="false">{html.escape(tag)}{number}</button>'
    )


def _render_card(repo: Repo, index: int = 0) -> str:
    """Render one project card: preview, name, description, links and date.

    ``index`` is the card's position in the grid, which the stylesheet turns
    into the delay before the card animates in.
    """
    name = html.escape(repo.name)
    pages_href = html.escape(repo.homepage, quote=True)
    tags = html.escape(" ".join(repo.tags), quote=True)
    lines = [
        f'      <article class="card" style="--i: {index}" data-tags="{tags}">',
        f'        <a class="shot" href="{pages_href}">{_render_thumb(repo)}</a>',
        '        <div class="body">',
        f'          <h3><a href="{pages_href}">{name}</a></h3>',
    ]
    if repo.description:
        lines.append(f'          <p class="desc">{html.escape(repo.description)}</p>')
    if repo.tags:
        shown = "".join(_chip(t) for t in repo.tags[:MAX_CARD_TAGS])
        lines.append(f'          <p class="card-tags">{shown}</p>')
    meta = [f'<a href="{pages_href}">live</a>']
    if repo.html_url:
        meta.append('<span class="sep" aria-hidden="true">·</span>')
        meta.append(f'<a href="{html.escape(repo.html_url, quote=True)}">source</a>')
    if repo.updated_date:
        meta.append(f'<span class="date">{html.escape(repo.updated_date)}</span>')
    lines.append(f'          <p class="meta">{"".join(meta)}</p>')
    lines.append("        </div>")
    lines.append("      </article>")
    return "\n".join(lines)


def render_html(
    repos: Sequence[Repo],
    generated_at: str,
    owner: str = "",
    tagline: str = "",
    profile: Optional[Profile] = None,
    year: str = "",
) -> str:
    """Render the projects as a standalone portfolio page.

    ``owner`` names the page heading and adds a link to the GitHub profile;
    ``tagline`` is the one-line self-description shown under it, defaulting to
    the account's bio; ``profile`` supplies the avatar (and a display name, when
    the account sets one); ``year`` adds a copyright line naming the same person
    as the heading. All dynamic values are HTML-escaped.
    """
    body = (
        "\n".join(_render_card(r, i) for i, r in enumerate(repos))
        if repos
        else EMPTY_STATE
    )
    heading = html.escape((profile.display_name if profile else "") or owner)
    profile_href = html.escape(
        (profile.html_url if profile else "") or f"https://github.com/{owner}",
        quote=True,
    )
    avatar = profile.avatar_url if profile else ""
    tagline = tagline or (profile.bio if profile else "")
    return PAGE_TEMPLATE.substitute(
        title=heading or "Published Repositories",
        heading=heading or "Published Repositories",
        avatar_html=(
            f'    <img class="avatar" src="{html.escape(avatar, quote=True)}" '
            f'alt="{heading}" width="88" height="88">\n'
            if avatar
            else ""
        ),
        tagline_html=(
            f'      <p class="tagline">{html.escape(tagline)}</p>\n' if tagline else ""
        ),
        profile_html=(
            f'      <p class="profile"><a href="{profile_href}">'
            f"github.com/{html.escape(owner)}</a></p>\n"
            if owner
            else ""
        ),
        section_label=(
            f'works (<span class="count">{len(repos)}</span>)' if repos else "works"
        ),
        tags_html=_render_tags(collect_tags(repos)),
        body=body,
        copyright_html=(
            f'    <p class="copyright">© {html.escape(year)} {heading}</p>\n'
            if year and heading
            else ""
        ),
        generated_at=html.escape(generated_at),
    )


# --------------------------------------------------------------------------- #
# Network layer — isolated for easy mocking in tests
# --------------------------------------------------------------------------- #

def _get_json(url: str, token: Optional[str] = None):
    """GET ``url`` from the GitHub API and decode the JSON body."""
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_repositories(user: str, token: Optional[str] = None) -> list[dict]:
    """Fetch all public repositories for ``user`` via the GitHub REST API.

    Uses pagination (100 per page, capped at MAX_PAGES). If ``token`` is given
    it is sent as a Bearer credential to raise the rate limit; the call also
    works unauthenticated. Only network I/O lives here so tests can substitute
    a fake implementation.
    """
    repos: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        batch = _get_json(
            f"{API_ROOT}/users/{urllib.parse.quote(user)}/repos"
            f"?per_page={PER_PAGE}&page={page}&type=public&sort=updated",
            token,
        )
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return repos


def fetch_profile(user: str, token: Optional[str] = None) -> Profile:
    """Fetch the account's avatar and display name.

    The portfolio still renders without them, so a failed lookup degrades to a
    bare Profile instead of aborting the build.
    """
    try:
        payload = _get_json(f"{API_ROOT}/users/{urllib.parse.quote(user)}", token)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"warning: could not fetch profile for {user}: {exc}", file=sys.stderr)
        return Profile(login=user)
    return parse_profile(payload if isinstance(payload, dict) else {}, user)


def fetch_preview(owner: str, repo: str, token: Optional[str] = None) -> str:
    """Look for a preview image in ``repo``, returning its URL or ''.

    PREVIEW_DIRS are probed in order and the first hit wins. Missing
    directories (404) and any other lookup failure are treated as "no preview":
    a project without one simply gets the monogram tile.
    """
    for directory in PREVIEW_DIRS:
        path = urllib.parse.quote(directory)
        try:
            entries = _get_json(
                f"{API_ROOT}/repos/{urllib.parse.quote(owner)}"
                f"/{urllib.parse.quote(repo)}/contents/{path}",
                token,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                continue
            print(f"warning: preview lookup failed for {repo}/{directory}: {exc}",
                  file=sys.stderr)
            return ""
        except (urllib.error.URLError, ValueError, OSError) as exc:
            print(f"warning: preview lookup failed for {repo}/{directory}: {exc}",
                  file=sys.stderr)
            return ""
        if not isinstance(entries, list):
            continue
        url = select_preview(entries)
        if url:
            return url
    return ""


def attach_previews(
    owner: str, repos: Sequence[Repo], token: Optional[str] = None
) -> list[Repo]:
    """Return ``repos`` with each entry's ``preview_url`` filled in where found."""
    return [
        replace(r, preview_url=fetch_preview(owner, r.name, token=token))
        for r in repos
    ]


def _relative_prefix(directory: Path, page_dir: Path) -> str:
    """Return ``directory`` as a URL path relative to the generated page."""
    return posixpath.normpath(
        os.path.relpath(directory, page_dir).replace(os.sep, "/")
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def build(
    user: str,
    readme_path: Path,
    html_path: Path,
    token: Optional[str] = None,
    now: Optional[datetime] = None,
    tagline: str = "",
    previews: bool = True,
    capture: bool = True,
    preview_dir: Optional[Path] = None,
    browser: Optional[str] = None,
    refresh: bool = False,
) -> list[Repo]:
    """Fetch, transform, and write both outputs. Returns the sorted repo list.

    Card images come from three places, in order: a preview file shipped by the
    project repository, an image captured from its published page (see
    ``capture_previews``), and finally the monogram tile drawn by the page
    itself.
    """
    timestamp = now or datetime.now(timezone.utc)
    generated_at = timestamp.strftime("%Y-%m-%d %H:%M UTC")
    raw = fetch_repositories(user, token=token)
    repos = sort_repositories(parse_repositories(raw))
    if previews:
        repos = attach_previews(user, repos, token=token)
    if capture:
        directory = preview_dir or html_path.parent / DEFAULT_PREVIEW_DIR
        repos = capture_previews.capture(
            repos,
            directory,
            browser=browser,
            refresh=refresh,
            url_prefix=_relative_prefix(directory, html_path.parent),
        )
    profile = fetch_profile(user, token=token)

    readme = readme_path.read_text(encoding="utf-8")
    readme_path.write_text(
        replace_readme_section(readme, render_markdown(repos)), encoding="utf-8"
    )

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(
        render_html(
            repos,
            generated_at,
            owner=user,
            tagline=tagline,
            profile=profile,
            year=timestamp.strftime("%Y"),
        ),
        encoding="utf-8",
    )
    return repos


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a repository index from public GitHub Pages homepages."
    )
    parser.add_argument("user", help="GitHub username to collect repositories for")
    parser.add_argument(
        "--readme",
        type=Path,
        default=Path("README.md"),
        help="Path to README.md containing the INDEX markers (default: README.md)",
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=Path("docs/index.html"),
        help="Path to the generated HTML page (default: docs/index.html)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token (optional). Defaults to $GITHUB_TOKEN if set.",
    )
    parser.add_argument(
        "--tagline",
        default="",
        help="Profile line shown under the name on the HTML page "
        "(defaults to the account's GitHub bio)",
    )
    parser.add_argument(
        "--no-previews",
        action="store_true",
        help="Skip the per-repository preview-image lookup (one API call per repo)",
    )
    parser.add_argument(
        "--no-capture",
        action="store_true",
        help="Do not capture card images from the projects' published pages",
    )
    parser.add_argument(
        "--preview-dir",
        type=Path,
        default=None,
        help="Directory for captured card images (default: <html dir>/previews)",
    )
    parser.add_argument(
        "--browser",
        default=None,
        help="Chrome/Chromium executable used for screenshots (default: autodetect)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-capture every card image, ignoring the manifest's freshness check",
    )
    args = parser.parse_args(argv)

    try:
        repos = build(
            args.user,
            args.readme,
            args.html,
            token=args.token,
            tagline=args.tagline,
            previews=not args.no_previews,
            capture=not args.no_capture,
            preview_dir=args.preview_dir,
            browser=args.browser,
            refresh=args.refresh,
        )
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"generated index for {len(repos)} repositories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
