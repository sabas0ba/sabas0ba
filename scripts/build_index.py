#!/usr/bin/env python3
"""Generate an index of public repositories that publish a homepage (GitHub Pages).

The script collects public repositories for a given GitHub user via the REST API,
keeps only those whose ``homepage`` field holds a non-empty URL, sorts them by
last update (newest first), and renders the result into two outputs:

- a marker-delimited section inside ``README.md`` (for the profile page)
- a static ``docs/index.html`` page (for GitHub Pages)

Only the Python standard library is used. Network access is confined to
``fetch_repositories``; all formatting logic is pure and unit-testable.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Iterable, Optional, Sequence

API_ROOT = "https://api.github.com"
USER_AGENT = "sabas0ba-index-builder"
PER_PAGE = 100
MAX_PAGES = 20  # hard cap: 20 * 100 = 2000 repos; prevents unbounded paging

README_START = "<!-- INDEX:START -->"
README_END = "<!-- INDEX:END -->"


@dataclass(frozen=True)
class Repo:
    """Minimal repository metadata used to build the index."""

    name: str
    description: str
    homepage: str
    updated_at: str  # ISO 8601 string as returned by the API
    html_url: str = ""  # repository page on github.com

    @property
    def updated_date(self) -> str:
        """Return the update timestamp as a YYYY-MM-DD string, or '' if unparsable."""
        try:
            dt = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return ""
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


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
            )
        )
    return repos


def sort_repositories(repos: Iterable[Repo]) -> list[Repo]:
    """Return repositories sorted by ``updated_at`` descending (newest first)."""
    return sorted(repos, key=lambda r: r.updated_at, reverse=True)


def _md_cell(text: str) -> str:
    """Escape characters that would break Markdown table structure."""
    return text.replace("|", "\\|").replace("\n", " ")


def _display_url(url: str) -> str:
    """Return a URL without its scheme prefix, for use as link text."""
    for prefix in ("https://", "http://"):
        if url.lower().startswith(prefix):
            return url[len(prefix):]
    return url


def render_markdown(repos: Sequence[Repo]) -> str:
    """Render the repository list as a Markdown table fragment.

    The Repository column links to the repository on github.com; the Pages
    column links to the published homepage. The fragment does not include the
    surrounding markers; see ``replace_readme_section`` for insertion.
    """
    if not repos:
        return "_No published repositories found._"
    lines = [
        "| Repository | Pages | Description | Updated |",
        "| --- | --- | --- | --- |",
    ]
    for r in repos:
        name = _md_cell(r.name)
        repo_cell = f"[{name}]({r.html_url})" if r.html_url else name
        pages_cell = f"[{_md_cell(_display_url(r.homepage))}]({r.homepage})"
        desc = _md_cell(r.description)
        lines.append(f"| {repo_cell} | {pages_cell} | {desc} | {r.updated_date} |")
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


# No external resources (fonts, CSS, JS) are referenced: the page must stay
# self-contained so publishing it adds no third-party dependencies.
PAGE_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$title</title>
  <style>
    :root {
      --bg: #101418;
      --text: #c9d1d9;
      --muted: #6a737d;
      --accent: #6cb6ff;
      --line: #2a3138;
    }
    * { box-sizing: border-box; }
    body {
      font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
      font-size: 0.92rem;
      max-width: 56rem;
      margin: 0 auto;
      padding: 2.5rem 1.25rem;
      line-height: 1.7;
      background: var(--bg);
      color: var(--text);
    }
    h1 { margin: 0; font-size: 1.05rem; font-weight: 600; color: var(--accent); }
    h1::before { content: "$$ "; color: var(--muted); }
    .tagline { margin: 0.2rem 0 0; color: var(--muted); }
    .tagline::before { content: "# "; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .profile { margin: 0.2rem 0 0; color: var(--muted); }
    .tablewrap { margin-top: 2rem; overflow-x: auto; border-top: 1px solid var(--line); }
    table { border-collapse: collapse; width: 100%; }
    th, td {
      text-align: left;
      padding: 0.55rem 1.2rem 0.55rem 0;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 400; }
    .date { color: var(--muted); white-space: nowrap; }
    footer { margin-top: 1.6rem; color: var(--muted); }
    footer::before { content: "// "; }
  </style>
</head>
<body>
  <header>
    <h1>$heading</h1>
$tagline_html$profile_html  </header>
  <main>
    <div class="tablewrap">
      <table>
        <thead>
          <tr><th>repository</th><th>pages</th><th>description</th><th>updated</th></tr>
        </thead>
        <tbody>
$body
        </tbody>
      </table>
    </div>
  </main>
  <footer>generated at $generated_at</footer>
</body>
</html>
""")


def render_html(
    repos: Sequence[Repo],
    generated_at: str,
    owner: str = "",
    tagline: str = "",
) -> str:
    """Render the repository list as a standalone HTML document.

    ``owner`` names the page heading and adds a link to the GitHub profile;
    ``tagline`` adds an optional subtitle line. All dynamic values are
    HTML-escaped. No external resources are referenced.
    """
    rows: list[str] = []
    for r in repos:
        name = html.escape(r.name)
        repo_href = html.escape(r.html_url, quote=True)
        pages_href = html.escape(r.homepage, quote=True)
        pages_text = html.escape(_display_url(r.homepage))
        desc = html.escape(r.description)
        date = html.escape(r.updated_date)
        repo_cell = f'<a href="{repo_href}">{name}</a>' if r.html_url else name
        rows.append(
            f"          <tr><td>{repo_cell}</td>"
            f'<td><a href="{pages_href}">{pages_text}</a></td>'
            f"<td>{desc}</td>"
            f'<td class="date">{date}</td></tr>'
        )
    body = (
        "\n".join(rows)
        if rows
        else '          <tr><td colspan="4">No published repositories found.</td></tr>'
    )
    owner_esc = html.escape(owner)
    tagline_esc = html.escape(tagline)
    profile_href = html.escape(f"https://github.com/{owner}", quote=True)
    return PAGE_TEMPLATE.substitute(
        title=owner_esc or "Published Repositories",
        heading=owner_esc or "Published Repositories",
        tagline_html=(
            f'    <p class="tagline">{tagline_esc}</p>\n' if tagline else ""
        ),
        profile_html=(
            f'    <p class="profile"><a href="{profile_href}">'
            f"github.com/{owner_esc}</a></p>\n"
            if owner
            else ""
        ),
        body=body,
        generated_at=html.escape(generated_at),
    )


# --------------------------------------------------------------------------- #
# Network layer — isolated for easy mocking in tests
# --------------------------------------------------------------------------- #

def fetch_repositories(user: str, token: Optional[str] = None) -> list[dict]:
    """Fetch all public repositories for ``user`` via the GitHub REST API.

    Uses pagination (100 per page, capped at MAX_PAGES). If ``token`` is given
    it is sent as a Bearer credential to raise the rate limit; the call also
    works unauthenticated. Only network I/O lives here so tests can substitute
    a fake implementation.
    """
    repos: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = (
            f"{API_ROOT}/users/{urllib.parse.quote(user)}/repos"
            f"?per_page={PER_PAGE}&page={page}&type=public&sort=updated"
        )
        request = urllib.request.Request(url)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.loads(response.read().decode("utf-8"))
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return repos


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
) -> list[Repo]:
    """Fetch, transform, and write both outputs. Returns the sorted repo list."""
    generated_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    raw = fetch_repositories(user, token=token)
    repos = sort_repositories(parse_repositories(raw))

    readme = readme_path.read_text(encoding="utf-8")
    readme_path.write_text(
        replace_readme_section(readme, render_markdown(repos)), encoding="utf-8"
    )

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(
        render_html(repos, generated_at, owner=user, tagline=tagline),
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
        help="Subtitle line shown under the heading of the HTML page",
    )
    args = parser.parse_args(argv)

    try:
        repos = build(
            args.user, args.readme, args.html, token=args.token, tagline=args.tagline
        )
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"generated index for {len(repos)} repositories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
