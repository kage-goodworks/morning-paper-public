"""テスト共通の材料。

ネットワーク・git・Slack APIは呼ばない。対象は純粋関数だけ（Spec「テスト方針」）。
"""

from __future__ import annotations

import pytest

from src.models import Article, Reactions, Story


def make_article(**kwargs) -> Article:
    base = dict(id="", url="https://example.invalid/a", title="見出し",
                source="src", category="ai",
                published_at="2026-09-01T09:00:00+09:00")
    base.update(kwargs)
    return Article(**base)


def make_story(**kwargs) -> Story:
    base = dict(id="s_000001", title="話題", category="ai", keywords=[],
                article_ids=["a1"], first_seen="2026-09-01",
                last_updated="2026-09-01", status="carryover",
                latest_url="https://example.invalid/a")
    base.update(kwargs)
    return Story(**base)


@pytest.fixture
def article():
    return make_article


@pytest.fixture
def story():
    return make_story


@pytest.fixture
def reactions():
    return Reactions
