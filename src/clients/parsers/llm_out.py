"""LLM出力の検証と分割。純粋関数。

検証せずにファイルへ書かない。形式不正は例外にせず ok=False で返し、
呼び出し側が降格・縮退を選べるようにする。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MARKER = re.compile(r"<!--\s*ARTICLE\s+(\d+)\s*-->")


@dataclass
class ArticleSplit:
    ok: bool
    bodies: list[str]
    reason: str = ""


def split_articles(text: str, expected: int) -> ArticleSplit:
    """`<!-- ARTICLE n -->` 区切りで本文を分割する。"""
    if not text or not text.strip():
        return ArticleSplit(False, [], "空の応答")

    parts = MARKER.split(text)
    # split の結果は [前置き, "1", 本文1, "2", 本文2, ...]
    if len(parts) < 3:
        return ArticleSplit(False, [], "ARTICLE区切りが見つからない")

    markers: list[int] = []
    bodies: list[str] = []
    for i in range(1, len(parts) - 1, 2):
        markers.append(int(parts[i]))
        body = parts[i + 1].strip()
        if body:
            bodies.append(body)

    expected_markers = list(range(1, expected + 1))
    if markers != expected_markers:
        return ArticleSplit(
            False, bodies,
            f"ARTICLE番号が不正（期待{expected_markers}、取得{markers}）")
    if len(bodies) != expected:
        return ArticleSplit(False, bodies,
                            f"記事数が一致しない（期待{expected}件、取得{len(bodies)}件）")
    return ArticleSplit(True, bodies)
