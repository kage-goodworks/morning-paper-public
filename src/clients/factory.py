"""依存注入の合流点。

呼び出し側は `Clients` だけを受け取り、Mock か Real かを知らない。
差し替えはここ1か所で行う。失敗注入も `Clients` を組み立てたあとに
一律でかぶせるので、個々の実装は失敗のことを知らない。
"""

from __future__ import annotations

from pathlib import Path

from .base import Clients
from .failure import FailureSpec, wrap
from .mock.context import MockContext


def build_mock(*, scenario: str = "normal", day: int = 1,
               out_dir: Path | None = None, phase: str = "morning",
               fail: str | None = None) -> Clients:
    """モックの束を組み立てる。"""
    from .mock import (extract as mock_extract, github as mock_github,
                       grok as mock_grok, llm as mock_llm,
                       reactions as mock_reactions, rss as mock_rss,
                       search as mock_search, slack as mock_slack,
                       stooq as mock_stooq, vault_git as mock_vault)

    ctx = MockContext(scenario=scenario, day=day, out_dir=out_dir, phase=phase)
    clients = Clients(
        feed=mock_rss.MockFeedClient(ctx),
        reactions=mock_reactions.MockReactionClient(ctx),
        extractors=mock_extract.build(ctx),
        releases=mock_github.MockReleaseClient(ctx),
        quotes=mock_stooq.MockQuoteClient(ctx),
        llms={"claude": mock_llm.MockClaude(ctx),
              "gemini": mock_llm.MockGemini(ctx)},
        social=mock_grok.MockSocialClient(ctx),
        search=mock_search.MockSearchClient(ctx),
        slack=mock_slack.MockSlackClient(ctx),
        notes=mock_vault.MockNoteStore(ctx),
    )
    clients.ctx = ctx  # type: ignore[attr-defined]
    return wrap(clients, FailureSpec(fail))


def build_real(*, fail: str | None = None,
               slack_channel: str | None = None,
               models: dict[str, str] | None = None) -> Clients:
    """本番の束を組み立てる。

    投稿先のチャンネルは秘密ではないので `config.yml` に置き、ここで渡す
    （NFR-7「設定値をコードに直書きしない」）。設定が空なら環境変数
    `SLACK_CHANNEL` へ落ちる。`models` は `config.yml` の `models`
    （呼ぶモデル名）。空ならコードの既定。
    """
    models = dict(models or {})
    from .real import (extract as real_extract, github as real_github,
                       grok as real_grok, llm as real_llm,
                       reactions as real_reactions, rss as real_rss,
                       search as real_search, slack as real_slack,
                       stooq as real_stooq, vault_git as real_vault)

    clients = Clients(
        feed=real_rss.RealFeedClient(),
        reactions=real_reactions.RealReactionClient(),
        extractors=[real_extract.RealRssFulltext(), real_extract.RealTrafilatura(),
                    real_extract.RealJinaReader(), real_extract.RealFirecrawl()],
        releases=real_github.RealReleaseClient(),
        quotes=real_stooq.RealQuoteClient(),
        llms={"claude": real_llm.RealClaude(
                  model=models.get("claude") or real_llm.CLAUDE_MODEL),
              "gemini": real_llm.RealGemini(
                  model=models.get("gemini") or real_llm.GEMINI_MODEL,
                  fallbacks=list(models.get("gemini_fallbacks") or []))},
        social=real_grok.RealSocialClient(),
        search=real_search.RealSearchClient(),
        slack=real_slack.RealSlackClient(channel=slack_channel or None),
        notes=real_vault.RealNoteStore(),
    )
    return wrap(clients, FailureSpec(fail))


def build(*, mock: bool = True, **kwargs) -> Clients:
    dry_run = bool(kwargs.pop("dry_run", False))
    # 本番だけが使う設定。モックは無視する
    slack_channel = kwargs.pop("slack_channel", None)
    models = kwargs.pop("models", None)
    if mock:
        return build_mock(**kwargs)

    clients = build_real(fail=kwargs.get("fail"),
                         slack_channel=slack_channel, models=models)
    if not dry_run:
        return clients

    # dry-run は取得だけ実クライアントを使い、外向きの書き込みは out/ へ逃がす。
    from .mock.context import MockContext
    from .mock.slack import MockSlackClient
    from .mock.vault_git import MockNoteStore

    ctx = MockContext(out_dir=kwargs.get("out_dir"))
    clients.slack = MockSlackClient(ctx)
    clients.notes = MockNoteStore(ctx)
    return clients
