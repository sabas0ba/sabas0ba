#!/usr/bin/env python3
"""Produce a card image for every published project, from its own Pages site.

Projects rarely ship a hand-made preview file, so this module generates one
locally instead of leaving the card blank:

1. if the published page embeds an animated image (GIF/APNG) that belongs to
   the project, that file is downloaded — an author-made demo animation beats
   any screenshot;
2. otherwise the page is rendered in headless Chrome and screenshotted.

Results are written next to the generated page (``docs/previews/``) and are
committed, so the portfolio serves its own images with no runtime dependency
on anything but this repository. ``manifest.json`` records which repository
revision each image was captured from, so a weekly rebuild only re-captures
projects that actually changed instead of churning every image.

Every step degrades to "no preview" rather than failing the build: without a
browser, without network, or on a page that refuses to render, the card simply
falls back to its monogram tile.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Sequence

USER_AGENT = "sabas0ba-index-builder"

# 16:9 matches the card's aspect-ratio, so screenshots are never cropped.
CAPTURE_WIDTH = 1200
CAPTURE_HEIGHT = 675

MANIFEST_NAME = "manifest.json"
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # keeps a stray large asset out of git history
PAGE_TIMEOUT = 30  # seconds, for plain HTTP fetches
BROWSER_TIMEOUT = 90  # seconds, for one headless render

# Only formats that are animated by definition are lifted out of a page: a
# .png/.webp <img> is usually a logo or a badge, which would make a worse card
# than a screenshot of the page itself.
ANIMATED_EXTENSIONS = (".gif", ".apng")

# Renders the page as a visitor with a dark system would see it. This sets the
# prefers-color-scheme query and nothing else — Chrome's own page-darkening is
# a separate feature and stays off, so a page with no dark styles comes out
# identical to its light capture and gets folded back into one file.
DARK_FLAGS = ("--blink-settings=preferredColorScheme=0",)
DARK_SUFFIX = "-dark"

# Chrome ships under several names depending on the image; the Playwright
# download is used by local sandboxes that have no system browser.
BROWSER_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/opt/pw-browsers/chromium",
)


# --------------------------------------------------------------------------- #
# Pure logic (no network, no subprocess) — fully unit-testable
# --------------------------------------------------------------------------- #

class _MediaFinder(HTMLParser):
    """Collect candidate image URLs from a page, in the order they appear."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.declared: list[str] = []  # og:image / twitter:image
        self.embedded: list[str] = []  # <img>, <source>

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key in ("og:image", "twitter:image") and values.get("content"):
                self.declared.append(values["content"])
        elif tag in ("img", "source"):
            # srcset lists "url width" pairs; the first url is enough here.
            srcset = values.get("srcset", "").split(",")[0].split()
            src = values.get("src") or (srcset[0] if srcset else "")
            if src:
                self.embedded.append(src)


def _extension(url: str) -> str:
    return os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()


def _same_project(candidate: str, page_url: str) -> bool:
    """Return True when ``candidate`` is served by the project itself.

    Only the project's own origin (and GitHub's asset domain, where README
    images live) is trusted: the file gets downloaded and committed here, so a
    third-party URL found in the page must not end up in this repository.
    """
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    page_host = (urllib.parse.urlparse(page_url).hostname or "").lower()
    return host == page_host or host.endswith(".githubusercontent.com")


def find_page_animation(page_html: str, page_url: str) -> str:
    """Return the URL of an animated image embedded in ``page_html``, or ''.

    Relative URLs are resolved against ``page_url``. A declared social image
    (og:image) wins over one found in the body, since the author picked it to
    represent the project.
    """
    finder = _MediaFinder()
    try:
        finder.feed(page_html)
    except Exception:  # malformed markup: use whatever was parsed so far
        pass
    for candidate in (*finder.declared, *finder.embedded):
        resolved = urllib.parse.urljoin(page_url, candidate.strip())
        if _extension(resolved) not in ANIMATED_EXTENSIONS:
            continue
        if _same_project(resolved, page_url):
            return resolved
    return ""


def safe_stem(name: str) -> str:
    """Return a filename-safe stem for a repository name.

    GitHub already restricts repository names to this alphabet; the filter is
    kept because the value becomes a path and a URL in the generated page.
    """
    stem = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name).strip(".-")
    return stem or "project"


def manifest_entry(repo, filename: str, source: str, dark: str = "") -> dict:
    """Describe a captured image, for the freshness check on the next run.

    ``dark`` names the dark-mode capture. The key is always written, empty when
    the project's page looks the same either way — most do — so that "there is
    no dark view" is on the record rather than indistinguishable from an entry
    written before dark views were captured at all.
    """
    return {
        "updated_at": repo.updated_at,
        "file": filename,
        "dark": dark,
        "source": source,
    }


def is_current(entry: Optional[dict], repo, out_dir: Path) -> bool:
    """Return True when a previously captured image can be reused as-is.

    An entry with no ``dark`` key predates dark captures; it is stale even at
    the same revision, which is what carries the change onto existing images
    without anyone asking for a refresh.
    """
    if not entry or entry.get("updated_at") != repo.updated_at:
        return False
    if "dark" not in entry:
        return False
    filename = entry.get("file") or ""
    if not filename or not (out_dir / filename).is_file():
        return False
    dark = entry.get("dark") or ""
    return not dark or (out_dir / dark).is_file()


def load_manifest(path: Path) -> dict:
    """Read the capture manifest, treating a missing or broken file as empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def prune(out_dir: Path, manifest: dict) -> list[str]:
    """Delete captured images no longer referenced by ``manifest``.

    Without this, a renamed or unpublished project would leave its image behind
    in the repository forever.
    """
    keep = {MANIFEST_NAME}
    for entry in manifest.values():
        if isinstance(entry, dict):
            keep.update(filter(None, (entry.get("file"), entry.get("dark"))))
    removed: list[str] = []
    for path in sorted(out_dir.glob("*")):
        if path.is_file() and path.name not in keep:
            path.unlink()
            removed.append(path.name)
    return removed


# --------------------------------------------------------------------------- #
# Network and browser layer — isolated for easy mocking in tests
# --------------------------------------------------------------------------- #

def find_browser(explicit: Optional[str] = None) -> str:
    """Return a usable Chrome/Chromium executable path, or '' if there is none."""
    for candidate in (explicit, os.environ.get("CHROME_BIN"), *BROWSER_CANDIDATES):
        if not candidate:
            continue
        resolved = shutil.which(candidate) or (
            candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else ""
        )
        if resolved:
            return resolved
    return ""


def fetch_page(url: str) -> str:
    """Download a published page as text, returning '' when it cannot be read."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=PAGE_TIMEOUT) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read(2 * 1024 * 1024).decode(charset, errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"warning: could not read {url}: {exc}", file=sys.stderr)
        return ""


def download_image(url: str, dest: Path) -> bool:
    """Download ``url`` to ``dest``; returns False on failure or oversize input."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=PAGE_TIMEOUT) as response:
            payload = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"warning: could not download {url}: {exc}", file=sys.stderr)
        return False
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        print(f"warning: skipping {url} ({len(payload)} bytes)", file=sys.stderr)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    return True


def screenshot(browser: str, url: str, dest: Path, dark: bool = False) -> bool:
    """Render ``url`` in headless Chrome and write a PNG to ``dest``.

    ``--virtual-time-budget`` fast-forwards the page's timers so fonts, lazy
    images and entry animations have settled before the frame is taken. With
    ``dark``, the page is rendered as it would appear to a visitor whose system
    is set to dark: this only answers the prefers-color-scheme query, so a page
    with no dark styles of its own comes out unchanged rather than machine-
    darkened.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={CAPTURE_WIDTH},{CAPTURE_HEIGHT}",
            "--virtual-time-budget=10000",
            *(DARK_FLAGS if dark else ()),
            f"--user-data-dir={profile}",
            f"--screenshot={dest}",
            url,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=BROWSER_TIMEOUT, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"warning: screenshot of {url} failed: {exc}", file=sys.stderr)
            return False
    if not dest.is_file() or dest.stat().st_size == 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        print(
            f"warning: screenshot of {url} produced no image "
            f"(exit {result.returncode}) {' '.join(detail)}",
            file=sys.stderr,
        )
        dest.unlink(missing_ok=True)
        return False
    return True


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def capture(
    repos: Sequence,
    out_dir: Path,
    browser: Optional[str] = None,
    refresh: bool = False,
    url_prefix: Optional[str] = None,
) -> list:
    """Give every repository without a preview one, captured from its own page.

    Repositories that already carry a ``preview_url`` (a file shipped by the
    project itself) are left untouched. Returns the repositories with their
    ``preview_url`` filled in as ``url_prefix``/<file>, which defaults to a
    path relative to ``out_dir``'s parent — i.e. what the generated page next
    to that directory has to write in a src attribute.
    """
    out_dir = Path(out_dir)
    prefix = (url_prefix or out_dir.name).rstrip("/")
    manifest_path = out_dir / MANIFEST_NAME
    previous = load_manifest(manifest_path)
    manifest: dict = {}
    executable = find_browser(browser)
    if not executable:
        print("warning: no Chrome/Chromium found; skipping screenshots", file=sys.stderr)

    result = []
    for repo in repos:
        if repo.preview_url:
            result.append(repo)
            continue
        entry = None if refresh else previous.get(repo.name)
        if is_current(entry, repo, out_dir):
            manifest[repo.name] = entry
        else:
            entry = _capture_one(repo, out_dir, executable)
            if entry:
                manifest[repo.name] = entry
        current = manifest.get(repo.name)
        if current:
            dark = current.get("dark")
            result.append(
                replace(
                    repo,
                    preview_url=f"{prefix}/{current['file']}",
                    preview_dark_url=f"{prefix}/{dark}" if dark else "",
                )
            )
        else:
            result.append(repo)

    if out_dir.is_dir():
        for name in prune(out_dir, manifest):
            print(f"removed stale preview {name}")
    if manifest or manifest_path.exists():
        save_manifest(manifest_path, manifest)
    return result


def _capture_one(repo, out_dir: Path, browser: str) -> Optional[dict]:
    """Capture one project: embedded animation first, screenshot second."""
    stem = safe_stem(repo.name)
    page = fetch_page(repo.homepage)
    if page:
        animation = find_page_animation(page, repo.homepage)
        if animation:
            filename = f"{stem}{_extension(animation)}"
            if download_image(animation, out_dir / filename):
                print(f"captured {repo.name} from {animation}")
                return manifest_entry(repo, filename, animation)
    if not browser or not page:
        # No page at all means the site is gone; screenshotting it would only
        # produce an error page.
        return None
    filename = f"{stem}.png"
    if not screenshot(browser, repo.homepage, out_dir / filename):
        return None
    print(f"captured {repo.name} from a screenshot of {repo.homepage}")
    return manifest_entry(
        repo, filename, "screenshot", dark=_capture_dark(repo, out_dir, browser, stem)
    )


def _capture_dark(repo, out_dir: Path, browser: str, stem: str) -> str:
    """Capture the dark-mode view, and return its filename — or '' if it matches.

    Most project pages have no dark styles, so the two captures come out
    byte-identical; keeping the second would double the repository's image
    weight for nothing, and the page falls back to the one image anyway.
    """
    dark_name = f"{stem}{DARK_SUFFIX}.png"
    dark_path = out_dir / dark_name
    if not screenshot(browser, repo.homepage, dark_path, dark=True):
        return ""
    try:
        same = dark_path.read_bytes() == (out_dir / f"{stem}.png").read_bytes()
    except OSError as exc:
        print(f"warning: could not compare {repo.name}'s captures: {exc}", file=sys.stderr)
        same = True
    if same:
        dark_path.unlink(missing_ok=True)
        return ""
    print(f"  {repo.name} also has a dark view")
    return dark_name
