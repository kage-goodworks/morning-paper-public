"""探索（FR-2 段階2）。Firecrawl の search を使う。

見つかったドメインは `discovered_sources.yml` へ候補として記録されるが、
**`config.yml` は自動で書き換えない**（記録は呼び出し側の `discover.py`）。
"""

from __future__ import annotations

import os

from ...models import Article
from . import http

ENDPOINT = "https://api.firecrawl.dev/v1/search"


class RealSearchClient:
    """`SearchClient`。落ちると探索の段階3へ進む。"""

    def __init__(self, limit: int = 10) -> None:
        self.limit = limit

    def search(self, query: str, date: str) -> list[Article]:
        key = os.environ.get("FIRECRAWL_API_KEY")
        if not key:
            raise RuntimeError("FIRECRAWL_API_KEY が設定されていません")

        payload = http.post_json(
            ENDPOINT,
            {"query": query, "limit": self.limit, "tbs": "qdr:d"},
            headers={"Authorization": f"Bearer {key}"},
            timeout=60.0)

        out: list[Article] = []
        for item in (payload or {}).get("data") or []:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if not url or not title:
                continue
            out.append(Article(
                id="", url=url, title=title, source="discover_search",
                category="industry", published_at=date,
                origin="discover_search"))
        return out
