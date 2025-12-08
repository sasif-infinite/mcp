"""
Crawl and Q&A for https://www.infinite.com/.
- Hard-coded to same-origin pages under https://www.infinite.com/
- Respects robots.txt and uses conservative limits to avoid aggressive crawling
- Stores an in-memory index for lightweight search/answering
"""

import re
from collections import deque
from html.parser import HTMLParser
from typing import Annotated
from urllib.parse import urljoin, urlparse
from urllib import robotparser

import httpx

# Hard-coded scope and limits for safety
BASE_URL = "https://www.infinite.com/"
MAX_PAGES = 40
MAX_DEPTH = 2
REQUEST_TIMEOUT = 10.0

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
    links = set()
    for href in re.findall(r'href=["\'](.*?)["\']', html, flags=re.IGNORECASE):
        absolute = _clean_url(urljoin(base, href))
        if absolute.startswith(BASE_URL) and _same_origin(absolute):
            links.add(absolute)
    return links


def _extract_text(html: str) -> str:
    # Drop script/style content quickly
    html = re.sub(
        r"<(script|style).*?>.*?</\\1>", " ", html, flags=re.IGNORECASE | re.DOTALL
    )
    parser = TextExtractor()
    parser.feed(html)
    return parser.get_text()


async def _fetch(url: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    try:
        resp = await client.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return url, resp.text
    except Exception:
        return None


async def crawl_and_index_infinite_site(
    max_pages: Annotated[int, "Maximum pages to crawl (caps at 40)"] = MAX_PAGES,
    max_depth: Annotated[int, "Maximum link depth to follow (caps at 2)"] = MAX_DEPTH,
) -> str:
    """
    Crawl https://www.infinite.com/ (same-origin), respect robots, and index text for Q&A.
    Limits: max_pages, max_depth, REQUEST_TIMEOUT per fetch.
    """
    global _index, _seen_urls
    _index = []
    _seen_urls = set()

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

    async with httpx.AsyncClient(headers={"User-Agent": "infinite-crawler/1.0"}) as client:
        while queue and len(_index) < max_pages:
            url, depth = queue.popleft()
            if depth > max_depth:
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

    return f"Indexed {len(_index)} page(s) from https://www.infinite.com/ (limit {max_pages})."


def _score(query_tokens: list[str], doc_text: str) -> int:
    text_lower = doc_text.lower()
    return sum(text_lower.count(t) for t in query_tokens)


def _summarize(text: str, tokens: list[str], chars: int = 320) -> str:
    text_lower = text.lower()
    best_pos = 0
    for t in tokens:
        pos = text_lower.find(t)
        if pos != -1:
            best_pos = pos
            break
    start = max(0, best_pos - chars // 2)
    end = min(len(text), start + chars)
    return text[start:end].strip()


async def answer_question_about_infinite(
    question: Annotated[str, "Question about content on https://www.infinite.com/"],
    top_k: Annotated[int, "Number of top hits to include"] = 3,
) -> str:
    """
    Answer a question using the crawled Infinite site content.
    Returns snippets and source URLs from the top matches.
    """
    if not _index:
        return "No index loaded. Run crawl_and_index_infinite_site first."

    query_tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9]+", question) if t]
    if not query_tokens:
        return "Please provide a non-empty question."

    scored = []
    for doc in _index:
        score = _score(query_tokens, doc["text"])
        if score > 0:
            scored.append((score, doc))

    if not scored:
        return "No relevant content found for that question."

    scored.sort(key=lambda x: x[0], reverse=True)
    top_hits = scored[:top_k]

    lines = ["Answer based on https://www.infinite.com/:", ""]
    for score, doc in top_hits:
        snippet = _summarize(doc["text"], query_tokens)
        lines.append(f"- Source: {doc['url']}")
        lines.append(f"  Snippet: {snippet}")
        lines.append(f"  Score: {score}")
    return "\n".join(lines)


async def crawl_then_answer_about_infinite(
    question: Annotated[str, "Question about content on https://www.infinite.com/"],
    max_pages: Annotated[int, "Maximum pages to crawl (caps at 40)"] = MAX_PAGES,
    max_depth: Annotated[int, "Maximum link depth to follow (caps at 2)"] = MAX_DEPTH,
    top_k: Annotated[int, "Number of top hits to include"] = 3,
) -> str:
    """
    Convenience tool: crawl then immediately answer the question.
    Useful when the index may be empty or stale.
    """
    crawl_summary = await crawl_and_index_infinite_site(max_pages=max_pages, max_depth=max_depth)
    answer = await answer_question_about_infinite(question=question, top_k=top_k)
    return f"{crawl_summary}\n\n{answer}"


def register_infinite_tools(app):
    """Register crawl and Q&A tools for the Infinite site."""
    app.tool(name="crawl_and_index_infinite_site")(crawl_and_index_infinite_site)
    app.tool(name="answer_question_about_infinite")(answer_question_about_infinite)
    app.tool(name="crawl_then_answer_about_infinite")(crawl_then_answer_about_infinite)
