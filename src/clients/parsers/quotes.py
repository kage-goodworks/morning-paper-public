"""株価の正規化。純粋関数。

- `parse` —— Symbol/Open/Close を持つ CSV（モックのフィクスチャ）
- `parse_chart` —— Yahoo Finance chart API の JSON（本番）

どちらも同じ `Quote` へ落とす。変化率は「終値 ÷ 前日終値」ではなく
「終値 ÷ 当日始値」で、朝の一覧で「昨日の取引でどう動いたか」を示す。
"""

from __future__ import annotations

import csv
import io
from typing import Any

from ...models import Quote


def _quote(symbol: str, open_: float, close: float) -> Quote:
    change = 0.0 if open_ == 0 else (close - open_) / open_ * 100
    return Quote(symbol=symbol.strip(), close=close, change_pct=round(change, 2))


def parse(csv_text: str) -> list[Quote]:
    if not csv_text or not csv_text.strip():
        return []
    out: list[Quote] = []
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    for row in reader:
        try:
            close = float(row["Close"])
            open_ = float(row.get("Open") or close)
        except (KeyError, TypeError, ValueError):
            continue  # フィールド欠落行は落とす
        out.append(_quote(row.get("Symbol", ""), open_, close))
    return out


def parse_chart(payload: Any) -> Quote | None:
    """Yahoo Finance `v8/finance/chart/<symbol>` の応答から最終取引日を取る。

    形が違う・値が欠けている場合は None。`meta.regularMarketPrice` を終値、
    その日の `indicators.quote[0].open` の最後の非 null を始値とする。
    """
    try:
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        symbol = str(meta.get("symbol") or "")
        close = float(meta["regularMarketPrice"])
        opens = result["indicators"]["quote"][0].get("open") or []
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    open_values = [float(v) for v in opens if v is not None]
    if not symbol or not open_values:
        return None
    return _quote(symbol, open_values[-1], close)
