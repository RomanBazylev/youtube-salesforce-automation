"""
Fetch fresh Salesforce content from multiple sources.

Sources:
  1. SalesforceBen.com — RSS feed
  2. Salesforce Developer Blog — RSS feed
  3. Reddit r/salesforce — JSON API (top + hot)
  4. Salesforce Release Notes — status.salesforce.com blog RSS
  5. Trailhead blog — RSS feed

Each source returns ContentItem objects.  pick_fresh_content() selects
an unused item and marks it in used_sources.json for deduplication.
"""

import json
import random
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

USED_SOURCES_PATH = Path("used_sources.json")
MAX_TRACKED = 500

HEADERS = {"User-Agent": "SalesforceYTBot/1.0 (content aggregation)"}

# ── Data model ─────────────────────────────────────────────────────────

@dataclass
class ContentItem:
    source: str        # e.g. "salesforceben", "devblog", "reddit", "releasenotes", "trailhead"
    url: str
    title: str
    summary: str       # short description / first paragraph
    full_text: str     # full article text (empty if not scraped yet)
    date: str          # ISO date string
    category: str      # e.g. "admin", "developer", "news", "release"


# ── Deduplication ──────────────────────────────────────────────────────

def _load_used() -> set:
    if USED_SOURCES_PATH.is_file():
        try:
            return set(json.loads(USED_SOURCES_PATH.read_text("utf-8")))
        except Exception:
            pass
    return set()


def _save_used(used: set) -> None:
    items = sorted(used)
    if len(items) > MAX_TRACKED:
        items = items[-MAX_TRACKED:]
    USED_SOURCES_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── RSS helpers ────────────────────────────────────────────────────────

def _parse_rss_items(xml_text: str, source_name: str, max_age_days: int = 60) -> List[ContentItem]:
    """Parse standard RSS 2.0 / Atom feed into ContentItem list."""
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return items

    # RSS 2.0: <channel><item>...
    rss_items = root.findall(".//item")
    # Atom: <entry>...
    atom_ns = "{http://www.w3.org/2005/Atom}"
    if not rss_items:
        rss_items = root.findall(f".//{atom_ns}entry")

    for item in rss_items:
        # RSS 2.0 fields
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        date_el = item.find("pubDate")

        # Atom fallbacks
        if title_el is None:
            title_el = item.find(f"{atom_ns}title")
        if link_el is None:
            link_el = item.find(f"{atom_ns}link")
        if desc_el is None:
            desc_el = item.find(f"{atom_ns}summary")
            if desc_el is None:
                desc_el = item.find(f"{atom_ns}content")
        if date_el is None:
            date_el = item.find(f"{atom_ns}published")
            if date_el is None:
                date_el = item.find(f"{atom_ns}updated")

        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue

        # Get link (RSS: text node; Atom: href attribute)
        url = ""
        if link_el is not None:
            url = (link_el.text or link_el.get("href", "")).strip()
        if not url:
            continue

        # Clean HTML from description
        summary = ""
        if desc_el is not None and desc_el.text:
            soup = BeautifulSoup(desc_el.text, "html.parser")
            summary = soup.get_text(separator=" ", strip=True)[:500]

        date_str = ""
        if date_el is not None and date_el.text:
            date_str = date_el.text.strip()

        items.append(ContentItem(
            source=source_name,
            url=url,
            title=title,
            summary=summary,
            full_text="",
            date=date_str,
            category="news",
        ))

    return items


def _fetch_rss(url: str, source_name: str) -> List[ContentItem]:
    """Fetch and parse a single RSS feed."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return _parse_rss_items(resp.text, source_name)
    except Exception as exc:
        print(f"[SOURCE] RSS {source_name} failed ({url}): {exc}")
        return []


# ── Source 1: SalesforceBen.com ────────────────────────────────────────

def fetch_salesforceben() -> List[ContentItem]:
    """Fetch latest articles from SalesforceBen.com RSS."""
    items = _fetch_rss("https://www.salesforceben.com/feed/", "salesforceben")
    for item in items:
        item.category = "tips"
    print(f"[SOURCE] SalesforceBen: {len(items)} articles")
    return items


# ── Source 2: Salesforce Developer Blog ────────────────────────────────

def fetch_developer_blog() -> List[ContentItem]:
    """Fetch latest posts from Salesforce Developer Blog."""
    # Primary: developer blog RSS (may 403 with bot UA)
    dev_headers = {
        **HEADERS,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    items = []
    for url in [
        "https://developer.salesforce.com/blogs/feed",
        "https://developer.salesforce.com/blogs/feed/atom",
        "https://developer.salesforce.com/blog/feed",
    ]:
        try:
            resp = requests.get(url, headers=dev_headers, timeout=30)
            resp.raise_for_status()
            items = _parse_rss_items(resp.text, "devblog")
            if items:
                break
        except Exception as exc:
            print(f"[SOURCE] RSS devblog failed ({url}): {exc}")
    for item in items:
        item.category = "developer"
    print(f"[SOURCE] Developer Blog: {len(items)} articles")
    return items


# ── Source 3: Reddit r/salesforce ──────────────────────────────────────

REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def fetch_reddit_salesforce() -> List[ContentItem]:
    """Fetch top and hot posts from r/salesforce."""
    items = []

    for endpoint in ["top", "hot"]:
        params = {"limit": 25}
        if endpoint == "top":
            params["t"] = "week"
        url = f"https://www.reddit.com/r/salesforce/{endpoint}/.json"
        try:
            time.sleep(random.uniform(1.5, 3.0))
            resp = requests.get(url, headers=REDDIT_HEADERS, params=params, timeout=15)
            if resp.status_code == 429:
                print(f"[SOURCE] Reddit rate limited on {endpoint}, skipping")
                continue
            if resp.status_code == 403:
                print(f"[SOURCE] Reddit forbidden on {endpoint}, skipping")
                continue
            resp.raise_for_status()
            data = resp.json()
            posts = data.get("data", {}).get("children", [])
            for post_wrap in posts:
                post = post_wrap.get("data", {})
                if post.get("removed_by_category"):
                    continue
                selftext = post.get("selftext", "")
                if selftext in ("[removed]", "[deleted]", ""):
                    continue
                if len(selftext) < 50:
                    continue
                title = post.get("title", "")
                if not title:
                    continue
                permalink = post.get("permalink", "")
                post_url = f"https://www.reddit.com{permalink}" if permalink else ""
                if not post_url:
                    continue
                score = post.get("score", 0)
                num_comments = post.get("num_comments", 0)

                items.append(ContentItem(
                    source="reddit",
                    url=post_url,
                    title=title,
                    summary=selftext[:500],
                    full_text=selftext,
                    date=datetime.fromtimestamp(
                        post.get("created_utc", 0), tz=timezone.utc
                    ).isoformat() if post.get("created_utc") else "",
                    category="discussion",
                ))
        except Exception as exc:
            print(f"[SOURCE] Reddit {endpoint} failed: {exc}")
            continue

    # Deduplicate by URL
    seen = set()
    unique = []
    for item in items:
        if item.url not in seen:
            seen.add(item.url)
            unique.append(item)
    items = unique

    print(f"[SOURCE] Reddit r/salesforce: {len(items)} posts")
    return items


# ── Source 4: Salesforce Release Notes / Status Blog ───────────────────

def fetch_release_notes() -> List[ContentItem]:
    """Fetch Salesforce release-related content from official blogs."""
    items = []
    # Salesforce main blog RSS
    blog_items = _fetch_rss("https://www.salesforce.com/blog/feed/", "sfblog")
    for item in blog_items:
        item.category = "release"
    items.extend(blog_items)

    # Admin blog (often has release notes summaries)
    admin_items = _fetch_rss("https://admin.salesforce.com/feed/", "sfadmin")
    for item in admin_items:
        item.category = "admin"
    items.extend(admin_items)

    print(f"[SOURCE] Salesforce Blogs: {len(items)} articles")
    return items


# ── Source 5: Trailhead blog ──────────────────────────────────────────

def fetch_trailhead() -> List[ContentItem]:
    """Fetch latest content from Trailhead blog."""
    trailhead_urls = [
        "https://trailhead.salesforce.com/blog/feed",
        "https://www.salesforce.com/trailblazer-community/feed/",
        "https://trailhead.salesforce.com/en/blog/feed",
        "https://www.salesforce.com/blog/category/trailhead/feed/",
    ]
    th_headers = {
        **HEADERS,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    items = []
    for url in trailhead_urls:
        try:
            resp = requests.get(url, headers=th_headers, timeout=30)
            resp.raise_for_status()
            items = _parse_rss_items(resp.text, "trailhead")
            if items:
                break
        except Exception as exc:
            print(f"[SOURCE] RSS trailhead failed ({url}): {exc}")
    for item in items:
        item.category = "learning"
    print(f"[SOURCE] Trailhead: {len(items)} articles")
    return items


# ── Full article scraping (for long-form videos) ──────────────────────

def scrape_full_article(item: ContentItem) -> ContentItem:
    """Fetch the full text of a ContentItem's URL for long-form scripts."""
    if item.full_text and len(item.full_text) > 200:
        return item  # already have full text (e.g. Reddit)

    if item.source == "reddit":
        return item  # Reddit posts have full_text from API

    try:
        resp = requests.get(item.url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise
        for tag in soup.find_all(["script", "style", "nav", "aside", "footer", "header"]):
            tag.decompose()

        # Try common content selectors
        content = None
        for selector in [
            "article",
            ("div", {"class": re.compile(r"entry.content|article.content|post.content", re.I)}),
            ("div", {"class": re.compile(r"blog.content|main.content", re.I)}),
            "main",
        ]:
            if isinstance(selector, tuple):
                content = soup.find(*selector)
            else:
                content = soup.find(selector)
            if content:
                break

        if content:
            text = content.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        item.full_text = "\n".join(lines)
        print(f"[SCRAPE] {item.source}: {len(item.full_text.split())} words from {item.url}")
    except Exception as exc:
        print(f"[SCRAPE] Failed {item.url}: {exc}")

    return item


# ── Content selection ──────────────────────────────────────────────────

def _fetch_all_sources() -> List[ContentItem]:
    """Fetch content from all sources. Each source is independent — failures are isolated."""
    all_items = []
    for fetcher in [
        fetch_salesforceben,
        fetch_developer_blog,
        fetch_reddit_salesforce,
        fetch_release_notes,
        fetch_trailhead,
    ]:
        try:
            all_items.extend(fetcher())
        except Exception as exc:
            print(f"[SOURCE] {fetcher.__name__} crashed: {exc}")
    return all_items


def pick_fresh_content(fmt: str = "short") -> Optional[ContentItem]:
    """
    Pick a single unused content item for video generation.

    Args:
        fmt: "short" — any source, summary is enough
             "long" — prefer sources with rich content, will scrape full text
    """
    all_items = _fetch_all_sources()
    if not all_items:
        print("[SOURCE] No content from any source!")
        return None

    used = _load_used()
    available = [item for item in all_items if item.url not in used]

    if not available:
        print("[SOURCE] All content used. Resetting oldest 50%.")
        used_list = sorted(used)
        half = len(used_list) // 2
        used = set(used_list[half:])
        _save_used(used)
        available = [item for item in all_items if item.url not in used]

    if not available:
        print("[SOURCE] Still no available content after reset!")
        return None

    # For long-form, prefer sources with richer content
    if fmt == "long":
        rich_sources = {"salesforceben", "devblog", "sfblog", "sfadmin"}
        rich = [item for item in available if item.source in rich_sources]
        if rich:
            available = rich

    # Pick randomly from available items
    chosen = random.choice(available)

    # For long-form, scrape full text
    if fmt == "long":
        chosen = scrape_full_article(chosen)

    # Mark as used
    used.add(chosen.url)
    _save_used(used)

    print(f"[PICK] {chosen.source}: {chosen.title[:60]}")
    return chosen


# ── Standalone test ────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Testing content sources ===\n")
    items = _fetch_all_sources()
    by_source = {}
    for item in items:
        by_source.setdefault(item.source, []).append(item)

    for source, source_items in sorted(by_source.items()):
        print(f"\n--- {source} ({len(source_items)} items) ---")
        for item in source_items[:3]:
            print(f"  [{item.category}] {item.title[:70]}")
            print(f"    URL: {item.url}")
            if item.summary:
                print(f"    Summary: {item.summary[:100]}...")

    print(f"\n=== Total: {len(items)} items from {len(by_source)} sources ===")

    # Test picking
    chosen = pick_fresh_content("short")
    if chosen:
        print(f"\nPicked for SHORT: [{chosen.source}] {chosen.title}")

    chosen_long = pick_fresh_content("long")
    if chosen_long:
        print(f"Picked for LONG: [{chosen_long.source}] {chosen_long.title}")
        if chosen_long.full_text:
            print(f"  Full text: {len(chosen_long.full_text.split())} words")
