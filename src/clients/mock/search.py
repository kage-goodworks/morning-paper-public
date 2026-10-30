"""探索（FR-2 段階2）のモック。"""

from __future__ import annotations

from ...models import Article
from .context import MockContext


class MockSearchClient:
    """`SearchClient` のモック。"""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def search(self, query: str, date: str) -> list[Article]:
        raw = self.ctx.read_json("search", "results.json") or {}
        out: list[Article] = []
        for item in raw.get("items") or []:
            out.append(Article(
                id="", url=item["url"], title=item["title"],
                source="discover_search",
                category=item.get("category") or "industry",
                published_at=date, origin="discover_search"))
        return out
