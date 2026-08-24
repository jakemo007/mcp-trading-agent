"""
tools/news.py — News-fetching tool (DuckDuckGo text search via `ddgs`).

The MCP server returns raw headlines + snippets; the LLM does
the actual sentiment interpretation.
"""

from __future__ import annotations

import logging
from typing import Any

from config import settings

logger = logging.getLogger(__name__)


def fetch_market_news(
    ticker: str,
    max_results: int | None = None,
) -> dict[str, Any]:
    """
    Fetch the latest news headlines & snippets for *ticker* using
    DuckDuckGo text search (via the ``ddgs`` package).

    Returns a JSON-serialisable dict:
      - query
      - articles: list of {title, snippet, url, source}
    """
    max_results = max_results or settings.default_max_news

    # Build a search query that surfaces financial / market news
    query = f"{ticker} stock market news latest"

    logger.info("Fetching news for '%s' (max %d results)", ticker, max_results)

    try:
        from ddgs import DDGS  # lazy import to keep startup fast

        articles: list[dict[str, str]] = []
        with DDGS() as ddgs:
            results = ddgs.text(
                query,
                max_results=max_results,
                timelimit=settings.news_time_limit,
            )
            for r in results:
                articles.append({
                    "title":   r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "url":     r.get("href", ""),
                    "source":  _extract_domain(r.get("href", "")),
                })

        return {
            "ticker":       ticker.upper(),
            "query":        query,
            "total_results": len(articles),
            "articles":     articles,
        }

    except ImportError:
        logger.error("The 'ddgs' package is not installed. pip install ddgs")
        return {
            "error": "ddgs package not installed. Run: pip install ddgs",
        }
    except Exception as exc:
        logger.exception("News fetch failed for '%s'", ticker)
        return {
            "error": f"News fetch failed: {exc}",
            "ticker": ticker.upper(),
        }


def _extract_domain(url: str) -> str:
    """Pull the domain name out of a URL for display."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except Exception:
        return url
