"""株価の取得。Yahoo Finance の chart API を使う。認証は不要。

もとは Stooq の CSV（`/q/l/?s=...&e=csv`）を使っていたが、2026-09 時点で
その口は 404 を返し、代替の `/q/d/l/` は JavaScript による人間確認の壁が
入っていて自動取得できない。Yahoo Finance の `v8/finance/chart` は非公式
だが鍵なしで JSON を返す。**落ちても株価帯が消えるだけで一覧は成立する**
ので、非公式でも許容している。

モジュール名は失敗注入（`--fail stooq`）やフィクスチャの置き場と揃えるため
据え置いた。
"""

from __future__ import annotations

from ...models import Quote
from ..parsers import quotes as quotes_parser
from . import http

ENDPOINT = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            "?range=5d&interval=1d")

#: Yahoo は `^` を URL エンコードしないと別の銘柄と解釈することがある
_ESCAPE = {"^": "%5E"}


class RealQuoteClient:
    """`QuoteClient`。落ちても株価帯が消えるだけで一覧は成立する。"""

    def fetch(self, symbols: list[str]) -> list[Quote]:
        out: list[Quote] = []
        for symbol in symbols:
            escaped = "".join(_ESCAPE.get(ch, ch) for ch in symbol)
            payload = http.fetch_json(ENDPOINT.format(symbol=escaped),
                                      timeout=15.0)
            quote = quotes_parser.parse_chart(payload)
            if quote is not None:
                out.append(quote)
        return out
