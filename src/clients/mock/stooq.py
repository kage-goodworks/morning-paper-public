"""株価のモック。Real と同じ `parsers.quotes.parse` を通す。"""

from __future__ import annotations

from ...models import Quote
from ..parsers import quotes as quotes_parser
from .context import MockContext


class MockQuoteClient:
    """`QuoteClient` のモック。"""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def fetch(self, symbols: list[str]) -> list[Quote]:
        csv_text = self.ctx.read_text("stooq", "quotes.csv") or ""
        return quotes_parser.parse(csv_text)
