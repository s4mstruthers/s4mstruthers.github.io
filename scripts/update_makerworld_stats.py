"""Fetch public Sams3DSolutions stats from MakerWorld and write them to stats.json.

Runs daily in GitHub Actions (.github/workflows/makerworld-stats.yml). The
portfolio page reads stats.json, which lives on the same site, so the browser's
cross-origin (CORS) restriction does not apply.

MakerWorld has no public API. This script loads the public profile page and
reads the model data embedded in it. If the page cannot be read or parsed, the
script exits with an error and leaves stats.json unchanged, so the website
keeps showing the last good numbers.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HANDLE = "Sams3DSolutions"
PROFILE_URL = f"https://makerworld.com/en/@{HANDLE}/upload"
OUT_PATH = Path(__file__).resolve().parent.parent / "stats.json"

# A normal desktop browser's headers. Bare scripts are more likely to be
# turned away by MakerWorld's bot protection.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Candidate key names for each figure. MakerWorld's internal field names are not
# documented, so several plausible spellings are accepted.
DOWNLOAD_KEYS = ("downloadCount", "download_count", "downloads")
PRINT_KEYS = ("printCount", "print_count", "prints")
TITLE_KEYS = ("title", "name")


def fetch(url: str) -> str:
    """Return the page HTML.

    curl_cffi is tried first because it imitates a real browser's TLS
    handshake, which bot protection checks. The standard library is the
    fallback.
    """
    try:
        from curl_cffi import requests as cffi

        resp = cffi.get(url, headers=HEADERS, impersonate="chrome", timeout=30)
        print(f"curl_cffi: HTTP {resp.status_code}, {len(resp.text)} chars")
        if resp.status_code == 200:
            return resp.text
    except ImportError:
        print("curl_cffi not installed; using urllib")

    import urllib.request

    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", "replace")
        print(f"urllib: HTTP {resp.status}, {len(body)} chars")
        return body


def first_key(d: dict, keys: tuple[str, ...]):
    """Value of the first key in `keys` present in `d`, or None."""
    for k in keys:
        if k in d:
            return d[k]
    return None


def find_designs(node, found: dict[str, dict]) -> None:
    """Walk parsed JSON and collect every object that looks like a model.

    A "model" is any dict with an id, a title, and integer download and print
    counts. Results are keyed by id, so a model listed in several places on the
    page (e.g. pinned and recent) is counted once.
    """
    if isinstance(node, dict):
        downloads = first_key(node, DOWNLOAD_KEYS)
        prints = first_key(node, PRINT_KEYS)
        title = first_key(node, TITLE_KEYS)
        design_id = node.get("id") or node.get("designId")
        if (
            isinstance(downloads, int)
            and isinstance(prints, int)
            and isinstance(title, str)
            and design_id is not None
        ):
            found[str(design_id)] = {
                "title": title,
                "downloads": downloads,
                "prints": prints,
            }
        for value in node.values():
            find_designs(value, found)
    elif isinstance(node, list):
        for item in node:
            find_designs(item, found)


def extract_json_blobs(html: str) -> list:
    """Parse every embedded JSON payload in the page.

    Next.js sites embed their data either in a <script id="__NEXT_DATA__"> tag
    (Pages Router) or as self.__next_f.push([...]) chunks (App Router); both are
    handled.
    """
    blobs = []
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if m:
        blobs.append(json.loads(m.group(1)))

    # App Router chunks are JSON strings containing more JSON; decode each one
    # and pull out any {...} objects that parse cleanly.
    for chunk in re.findall(r"self\.__next_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        for part in payload:
            if not isinstance(part, str):
                continue
            for obj in re.findall(r"\{.*\}", part, re.S):
                try:
                    blobs.append(json.loads(obj))
                except json.JSONDecodeError:
                    pass
    return blobs


def describe_page(html: str) -> None:
    """Print clues for debugging when nothing could be extracted."""
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    print("Page title:", title.group(1).strip() if title else "(none)")
    print("Has __NEXT_DATA__:", "__NEXT_DATA__" in html)
    print("Has __next_f:", "__next_f" in html)
    print("Looks like a bot challenge:", any(
        s in html for s in ("cf-chl", "challenge-platform", "Just a moment")
    ))
    for key in DOWNLOAD_KEYS + PRINT_KEYS:
        print(f"Occurrences of '{key}':", html.count(key))
    m = re.search(r".{0,300}downloadCount.{0,300}", html, re.S)
    if m:
        print("Context around downloadCount:\n", m.group(0))


def main() -> int:
    html = fetch(PROFILE_URL)

    designs: dict[str, dict] = {}
    for blob in extract_json_blobs(html):
        find_designs(blob, designs)

    if not designs:
        print("ERROR: no model data found on the page.")
        describe_page(html)
        return 1

    stats = {
        "source": PROFILE_URL,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": len(designs),
        "downloads": sum(d["downloads"] for d in designs.values()),
        "prints": sum(d["prints"] for d in designs.values()),
        "per_model": dict(sorted(designs.items())),
    }

    # Keep the old file if the counts have not changed, so the daily run does
    # not create a commit just because the timestamp moved.
    if OUT_PATH.exists():
        old = json.loads(OUT_PATH.read_text())
        keys = ("models", "downloads", "prints", "per_model")
        if all(old.get(k) == stats[k] for k in keys):
            print("No change in stats.")
            return 0

    OUT_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n")
    print(
        f"Wrote {OUT_PATH.name}: {stats['models']} models, "
        f"{stats['downloads']} downloads, {stats['prints']} prints"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
