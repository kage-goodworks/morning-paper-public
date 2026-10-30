"""無料で取れる反応のモック（HN・はてブ・Reddit）。

生レスポンスを読み、**Real と同じ `parsers.reactions.merge` を通す**。
"""

from __future__ import annotations

from ...models import Article, Reactions
from ..parsers import reactions as reactions_parser
from .context import MockContext


class MockReactionClient:
    """`ReactionClient` のモック。"""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def fetch(self, article: Article) -> Reactions:
        raw = self.ctx.read_json("reactions", f"{article.url_hash}.json")
        if not raw:
            return Reactions()
        # 週次レビューは「収集当時」ではなく「現在」の反応を取り直す工程なので、
        # 時間が経って伸びた値を返す（FR-11 の変化検出を踏むため）。
        if self.ctx.phase == "weekly":
            raw = raw.get("after") or raw
        return reactions_parser.merge(raw.get("hn"), raw.get("hatena"),
                                      raw.get("reddit"))
