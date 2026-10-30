"""Grok（X）レスポンスの正規化。純粋関数。

`citations` が空でも正当な結果として扱う。反応を創作させないため、
summary は citations が空のとき None にする（AC-17）。
"""

from __future__ import annotations

from typing import Any

from ...models import SocialResult

NO_REACTION = "目立った反応は見つからなかった"


def parse(payload: dict[str, Any] | None) -> SocialResult:
    if not payload:
        return SocialResult(summary=None, citations=[], topics=[])
    citations = [c for c in (payload.get("citations") or []) if c]
    topics = [t for t in (payload.get("topics") or []) if t]
    summary = payload.get("summary")
    if not citations:
        summary = None
    return SocialResult(summary=summary, citations=citations, topics=topics)


def summary_text(result: SocialResult) -> str:
    """一覧・記事へ出す文字列。出典が無いときは定型文にする。"""
    if not result.citations or not result.summary:
        return NO_REACTION
    return result.summary
