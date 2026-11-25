"""Async search providers and static document retrieval for the research runtime."""
import asyncio
from dataclasses import dataclass
from html import escape
import os

import httpx

from .tool_result import ToolFailure


def external_content(source: str, text: str) -> str:
    return (f'<external_web_content source="{escape(source, quote=True)}">\n'
            f'{escape(text, quote=False)}\n</external_web_content>')


@dataclass(frozen=True)
class SearchProvider:
    name: str
    endpoint: str
    key_env: str
    result_field: str
    url_field: str
    snippet_field: str

    async def search(self, client: httpx.AsyncClient, query: str, limit: int) -> list:
        key = os.environ.get(self.key_env)
        if not key:
            raise ToolFailure(f"Set {self.key_env} to enable {self.name} search.")
        if self.name == "tavily":
            headers = {"Authorization": f"Bearer {key}"}
            payload = {"query": query, "max_results": limit, "include_raw_content": False}
        else:
            headers = {"X-API-KEY": key}
            payload = {"q": query, "num": limit}
        response = await client.post(self.endpoint, headers=headers, json=payload)
        response.raise_for_status()
        rows = response.json().get(self.result_field)
        if not isinstance(rows, list):
            raise ToolFailure(f"{self.name} did not return a search result list.")
        return rows[:limit]


PROVIDERS = {
    "tavily": SearchProvider("tavily", "https://api.tavily.com/search", "TAVILY_API_KEY",
                             "results", "url", "content"),
    "serper": SearchProvider("serper", "https://google.serper.dev/search", "SERPER_API_KEY",
                             "organic", "link", "snippet"),
}


def search_provider() -> SearchProvider:
    name = os.environ.get("RESEARCH_SEARCH_PROVIDER", "tavily").strip().lower()
    if name not in PROVIDERS:
        raise ToolFailure("RESEARCH_SEARCH_PROVIDER must be tavily or serper.")
    return PROVIDERS[name]


def normalize_web_args(name: str, args: dict) -> dict:
    if name == "web_search":
        query = args["query"].strip()
        if not query:
            raise ToolFailure("Search query must contain text.")
        return {"query": query, "max_results": max(1, min(10, args.get("max_results", 5)))}
    url = httpx.URL(args["url"].strip())
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ToolFailure("web_fetch requires an HTTP(S) URL without embedded credentials.")
    return {"url": str(url.copy_with(fragment=None))}


async def web_search(query: str, max_results: int) -> str:
    provider = search_provider()
    try:
        async with asyncio.timeout(15), httpx.AsyncClient(timeout=15) as client:
            rows = await provider.search(client, query, max(1, min(10, max_results)))
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise TimeoutError("web_search request timed out.") from exc
    entries = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(provider.url_field), str):
            continue
        url = row[provider.url_field]
        if not url.startswith(("http://", "https://")):
            continue
        title = str(row.get("title") or "")[:200]
        snippet = str(row.get(provider.snippet_field) or "")[:600]
        entry = f"{title}\n{url}\n{snippet}"
        if len(external_content(provider.name, "\n\n".join([*entries, entry]))) <= 4000:
            entries.append(entry)
    return external_content(provider.name, "\n\n".join(entries) or "No displayable search results.")


def extract_page(body: bytes, source: str) -> str:
    from trafilatura import extract
    from lxml import etree, html

    article = extract(body, url=source, output_format="markdown", include_links=True, include_comments=False)
    if article:
        return article
    # Short, valid documents can fall below the article extractor's heuristics.
    try:
        tree = html.document_fromstring(body)
    except (ValueError, etree.ParserError):
        return ""
    for node in tree.xpath("//head|//script|//style|//template|//noscript|//*[@hidden]"):
        node.drop_tree()
    return " ".join(tree.text_content().split())


async def web_fetch(url: str) -> str:
    hint = "For rendered content or interaction, the explore agent can inspect the page in a browser."
    try:
        async with asyncio.timeout(20), httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            async with client.stream("GET", url) as response:
                if response.status_code in (403, 503):
                    raise ToolFailure(f"HTTP {response.status_code} fetching the page. {hint}")
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";")[0].lower().strip()
                html = mime in ("text/html", "application/xhtml+xml", "")
                if not html and not (mime.startswith("text/") or mime in ("application/json", "application/xml")):
                    raise ToolFailure(f"Unsupported document type: {mime}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 8 * 1024 * 1024:
                        raise ToolFailure("Document exceeds the 8 MiB fetch limit.")
                    body.extend(chunk)
                source = str(response.url)
                if html and body:
                    text = await asyncio.to_thread(extract_page, bytes(body), source)
                else:
                    text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise TimeoutError("web_fetch request timed out.") from exc
    if not text or not text.strip() or (len(text) < 300 and any(marker in text.lower() for marker in
            ("enable javascript", "requires javascript", "please turn on javascript", "loading...", "loading…"))):
        raise ToolFailure(f"No usable static document text. {hint}")
    return external_content(source, text)
