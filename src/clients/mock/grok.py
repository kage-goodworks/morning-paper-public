"""Grok（X）のモック。

**対象の絞り込みとバッチ分割はモックしない。**どの記事を対象にするか、
何件ずつまとめるかは本番と同じ `src/reactions.py` が決め、ここは渡された
まとまりに対して返すだけ。

`citations` が空のケースを必ず含める（AC-17）。
"""

from __future__ import annotations

from ...models import Article, SocialResult
from ..parsers import social as social_parser
from .context import MockContext


class MockSocialClient:
    """`SocialClient` のモック。"""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def reactions(self, articles: list[Article]) -> dict[str, SocialResult]:
        raw = self.ctx.read_json("grok", "reactions.json") or {}
        out: dict[str, SocialResult] = {}
        for i, article in enumerate(articles):
            # 3件に1件は出典0件にして、創作させない経路を毎回踏ませる
            key = "empty" if i % 3 == 2 else "default"
            out[article.url] = social_parser.parse(raw.get(key))
        return out

    def topics(self, date: str) -> list[str]:
        raw = self.ctx.read_json("grok", "topics.json") or {}
        return list(raw.get("topics") or [])

    def discover(self, date: str) -> list[Article]:
        raw = self.ctx.read_json("grok", "discover.json") or {}
        out: list[Article] = []
        for item in raw.get("items") or []:
            out.append(Article(
                id="", url=item["url"], title=item["title"],
                source="discover_x", category=item.get("category") or "ai",
                published_at=date, origin="discover_x"))
        return out
