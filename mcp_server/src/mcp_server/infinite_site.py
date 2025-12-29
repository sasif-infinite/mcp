"""
Hard-coded crawler for https://www.infinite.com/ and same-origin child pages.
- Respects robots.txt, conservative depth/page limits, and keeps a lightweight index.
- Returns a concise summary of crawled pages so agents can answer from the returned content.
- Persists the most recent index to disk so results survive process restarts when writes are allowed.
"""

import json
import re
import asyncio
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx

# Hard-coded scope and limits for safety (kept small for quick responses)
BASE_URL = "https://www.infinite.com/"
MAX_PAGES = 40
MAX_DEPTH = 2
REQUEST_TIMEOUT = 8.0
INDEX_PATH = Path(__file__).resolve().parent / "data" / "infinite_index.json"
INDEX_VERSION = 1

# In-memory store for crawled pages
_index: list[dict[str, str]] = []
_seen_urls: set[str] = set()


class TextExtractor(HTMLParser):
    """Lightweight HTML text extractor for headings/body content."""

    def __init__(self) -> None:
        super().__init__()
        self._texts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self._texts.append(stripped)

    def get_text(self) -> str:
        return " ".join(self._texts)


def _same_origin(url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(BASE_URL)
    return parsed.scheme == base.scheme and parsed.netloc == base.netloc


def _clean_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _extract_links(html: str, base: str) -> set[str]:
    html = html[:200000]  # Limit parsing to keep regex fast on very large pages.
    links = set()
    for href in re.findall(r'href=["\'](.*?)["\']', html, flags=re.IGNORECASE):
        absolute = _clean_url(urljoin(base, href))
        if absolute.startswith(BASE_URL) and _same_origin(absolute):
            links.add(absolute)
    return links


def _extract_text(html: str) -> str:
    # Trim to avoid pathological backtracking on very large pages.
    html = html[:200000]
    # Drop script/style content quickly (non-greedy to avoid runaway regex work).
    html = re.sub(
        r"(?is)<(script|style)[^>]*?>.*?</\\1>", " ",
        html,
    )
    # Strip remaining tags to simplify the parser workload.
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    parser = TextExtractor()
    parser.feed(html)
    return parser.get_text()


async def _fetch(url: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    try:
        resp = await client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return url, resp.text
    except Exception:
        return None


def _clamp_limits(max_pages: int, max_depth: int) -> tuple[int, int]:
    capped_pages = max(1, min(max_pages, MAX_PAGES))
    capped_depth = max(0, min(max_depth, MAX_DEPTH))
    return capped_pages, capped_depth


def _save_index_to_disk() -> bool:
    if not _index:
        return False
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "base_url": BASE_URL,
            "pages": _index,
        }
        INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def _load_index_from_disk() -> None:
    global _index
    if _index:
        return
    if not INDEX_PATH.exists():
        return
    try:
        payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if payload.get("version") != INDEX_VERSION:
            return
        pages = payload.get("pages") or []
        if isinstance(pages, list):
            _index = [
                {"url": p.get("url", ""), "text": p.get("text", "")} for p in pages
            ]
    except Exception:
        # If loading fails, continue with empty index
        _index = []


async def crawl_infinite_site(
    max_pages: Annotated[int, "Maximum pages to crawl (caps at 40)"] = MAX_PAGES,
    max_depth: Annotated[int, "Maximum link depth to follow (caps at 2)"] = MAX_DEPTH,
) -> dict:
    """
    Crawl https://www.infinite.com/ (same-origin), respect robots, and summarize results.
    Limits: max_pages, max_depth, REQUEST_TIMEOUT per fetch.
    """
    global _index, _seen_urls
    cached_index: list[dict[str, str]] = []
    _load_index_from_disk()
    if _index:
        cached_index = list(_index)

    _index = []
    _seen_urls = set()

    capped_pages, capped_depth = _clamp_limits(max_pages, max_depth)

    async def _run_crawl() -> dict:
        robots = robotparser.RobotFileParser()
        robots.set_url(urljoin(BASE_URL, "/robots.txt"))
        try:
            robots.read()
        except Exception:
            # If robots.txt is unreachable, default to cautious allow
            pass

        queue: deque[tuple[str, int]] = deque()
        start_url = _clean_url(BASE_URL)
        queue.append((start_url, 0))
        _seen_urls.add(start_url)

        async with httpx.AsyncClient(
            headers={"User-Agent": "infinite-crawler/1.0"}
        ) as client:
            while queue and len(_index) < capped_pages:
                url, depth = queue.popleft()
                if depth > capped_depth:
                    continue
                if robots.can_fetch("*", url) is False:
                    continue

                fetched = await _fetch(url, client)
                if not fetched:
                    continue
                _, html = fetched
                text = _extract_text(html)
                if text:
                    _index.append({"url": url, "text": text})

                for link in _extract_links(html, url):
                    if link not in _seen_urls:
                        _seen_urls.add(link)
                        queue.append((link, depth + 1))

        persisted = _save_index_to_disk()
        used_cached_index = False
        if not _index and cached_index:
            _index.extend(cached_index)
            used_cached_index = True

        # Prepare concise snippets for the caller to answer from without extra tool calls.
        summaries: list[dict[str, str | int]] = []
        for page in _index[: min(10, len(_index))]:
            snippet = page["text"][:320].strip()
            summaries.append({"url": page["url"], "snippet": snippet})

        return {
            "status": "ok" if _index else "empty",
            "pages_indexed": len(_index),
            "max_pages": capped_pages,
            "max_depth": capped_depth,
            "pages": summaries,
            "persisted_to_disk": persisted,
            "used_cached_index": used_cached_index,
            "note": "Mcp Server Crawl: Infinite.com same-origin crawl.",
        }

    try:
        return await asyncio.wait_for(_run_crawl(), timeout=40.0)
    except asyncio.TimeoutError:
        summaries: list[dict[str, str | int]] = []
        for page in cached_index[: min(10, len(cached_index))]:
            snippet = page["text"][:320].strip()
            summaries.append({"url": page["url"], "snippet": snippet})
        return {
            "status": "timeout",
            "pages_indexed": len(cached_index),
            "max_pages": capped_pages,
            "max_depth": capped_depth,
            "pages": summaries,
            "persisted_to_disk": False,
            "used_cached_index": bool(cached_index),
            "note": "Mcp Server Crawl: Infinite.com same-origin crawl (timed out; returned cached index if available).",
        }


def register_infinite_tools(app):
    """Register the crawl tool for the Infinite site."""
    app.tool(name="crawl")(crawl_infinite_site)
