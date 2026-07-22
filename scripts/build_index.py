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
    Archived/fork status is not considered here.
    """
    repos: list[Repo] = []
    for item in payload:
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
            )
        )
    return repos


def sort_repositories(repos: Iterable[Repo]) -> list[Repo]:
    """Return repositories sorted by ``updated_at`` descending (newest first)."""
    return sorted(repos, key=lambda r: r.updated_at, reverse=True)


def _md_cell(text: str) -> str:
    """Escape characters that would break Markdown table structure."""
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(repos: Sequence[Repo]) -> str:
    """Render the repository list as a Markdown table fragment.

    The fragment does not include the surrounding markers; see
    ``replace_readme_section`` for insertion.
    """
    if not repos:
        return "_No published repositories found._"
    lines = [
        "| Repository | Description | Updated |",
        "| --- | --- | --- |",
    ]
    for r in repos:
        name = _md_cell(r.name)
        desc = _md_cell(r.description)
        lines.append(f"| [{name}]({r.homepage}) | {desc} | {r.updated_date} |")
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


def render_html(repos: Sequence[Repo], generated_at: str) -> str:
    """Render the repository list as a standalone HTML document.

    All dynamic values are HTML-escaped. ``generated_at`` is inserted verbatim
    and is expected to be a caller-controlled timestamp string.
    """
    rows: list[str] = []
    for r in repos:
        name = html.escape(r.name)
        href = html.escape(r.homepage, quote=True)
        desc = html.escape(r.description)
        date = html.escape(r.updated_date)
        rows.append(
            f'      <tr><td><a href="{href}">{name}</a></td>'
            f"<td>{desc}</td>"
            f'<td class="date">{date}</td></tr>'
        )
    body = (
        "\n".join(rows)
        if rows
        else '      <tr><td colspan="3">No published repositories found.</td></tr>'
    )
    gen = html.escape(generated_at)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "  <title>Published Repositories</title>\n"
        "  <style>\n"
        "    body { font-family: system-ui, sans-serif; max-width: 48rem;"
        " margin: 2rem auto; padding: 0 1rem; line-height: 1.6; }\n"
        "    table { border-collapse: collapse; width: 100%; }\n"
        "    th, td { text-align: left; padding: 0.4rem 0.8rem 0.4rem 0;"
        " border-bottom: 1px solid #eee; vertical-align: top; }\n"
        "    .date { color: #888; font-size: 0.9em; white-space: nowrap; }\n"
        "    footer { margin-top: 2rem; color: #888; font-size: 0.85em; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>Published Repositories</h1>\n"
        "  <table>\n"
        "    <thead>\n"
        "      <tr><th>Repository</th><th>Description</th><th>Updated</th></tr>\n"
        "    </thead>\n"
        "    <tbody>\n"
        f"{body}\n"
        "    </tbody>\n"
        "  </table>\n"
        f"  <footer>Generated at {gen}</footer>\n"
        "</body>\n"
        "</html>\n"
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
    html_path.write_text(render_html(repos, generated_at), encoding="utf-8")
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
    args = parser.parse_args(argv)

    try:
        repos = build(args.user, args.readme, args.html, token=args.token)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"generated index for {len(repos)} repositories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
