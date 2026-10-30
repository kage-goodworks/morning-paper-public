"""フィードのモック。

`tests/fixtures/rss/<day>/<source>.xml` の生XMLを読み、**Real と同じ
`parsers.feed.parse` を通す**。正規化処理をモック側へ写さないため、
フィードの形が変わったときに直す場所は1か所で済む。
"""

from __future__ import annotations

from typing import Any

from ...models import Article
from ..parsers import feed as feed_parser
from .context import MockContext


class MockFeedClient:
    """`FeedClient` のモック。"""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def fetch_articles(self, source: dict[str, Any]) -> list[Article]:
        source_id = source.get("id") or ""
        path = self.ctx.feed_dir / f"{source_id}.xml"
        if not path.exists():
            # そのソースに当日の更新が無い状態。例外ではなく空を返す。
            return []
        return feed_parser.parse(
            path.read_bytes(),
            source_id=source_id,
            default_category=source.get("category") or "industry",
            fulltext=bool(source.get("fulltext")),
            origin=source.get("origin") or "config",
            kind="gallery" if source.get("type") == "gallery" else "article",
        )
