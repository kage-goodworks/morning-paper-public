"""反応の取得（副作用あり）。

無料で取れる反応（HN・はてブ・Reddit）は常に取る。X（Grok）は
`reactions.x_enabled` が `true` のときだけ（既定 `false`、FR-7）。

**対象の絞り込みとバッチ分割はモックしない。**どの記事を対象にするか、
何件ずつまとめるかはここが決め、モックは渡されたまとまりに答えるだけ。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .clients.parsers import social as social_parser
from .models import Article, SocialResult
from .pipeline import Run


def fetch_free(run: Run, articles: list[Article]) -> list[Article]:
    """無料の反応を埋める。落ちても反応量0で続行する。"""
    def one(article: Article) -> Article:
        result = run.guarded(
            lambda: run.clients.reactions.fetch(article),
            target="反応の取得（HN・はてブ・Reddit）",
            impact="その記事の反応量が0として扱われます",
            default=None, status="", notify=False)  # 記事ごとは知らせない
        if result is not None:
            article.reactions = result
        return article

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, articles))


def is_x_target(article: Article, *, categories: list[str],
                thresholds: dict[str, int]) -> bool:
    """Xの反応を取る対象か判定する（FR-7「反応を取る対象の絞り込み」）。

    カテゴリが対象で、かつ反応量がしきい値のいずれかを超えること。
    純粋関数。
    """
    if article.category not in categories:
        return False
    reactions = article.reactions
    return (reactions.hn_points >= int(thresholds.get("hn_points", 10 ** 9))
            or reactions.hatena >= int(thresholds.get("hatena", 10 ** 9))
            or reactions.reddit >= int(thresholds.get("reddit", 10 ** 9)))


def batches(items: list[Article], size: int) -> list[list[Article]]:
    """1コールにまとめる単位へ分ける。純粋関数。"""
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def fetch_x(run: Run, articles: list[Article]) -> dict[str, SocialResult]:
    """Xの反応を取る。`x_enabled` が false なら何もしない（AC-19）。"""
    if not run.config.get("reactions.x_enabled", False):
        run.note_status("X: 無効のため取得していません（HN・はてブ・Redditの反応で構成）")
        return {}

    categories = list(run.config.get("reactions.target_categories", []) or [])
    thresholds = dict(run.config.get("reactions.thresholds", {}) or {})
    targets = [a for a in articles
               if is_x_target(a, categories=categories, thresholds=thresholds)]

    if not targets:
        run.note_status("X: しきい値を超える記事がありませんでした")
        return {}

    out: dict[str, SocialResult] = {}
    size = int(run.config.get("reactions.batch_size", 5))
    for batch in batches(targets, size):
        result = run.guarded(
            lambda batch=batch: run.clients.social.reactions(batch),
            target="xAI（X上の反応）",
            impact="X上の反応が載りません",
            default={})
        out.update(result or {})

    run.note_status(f"X: {len(targets)}件の記事について反応を取得しました")
    return out


def fetch_x_topics(run: Run) -> list[str]:
    """「昨日Xで話題になったこと」。`x_enabled` のときだけ。"""
    if not run.config.get("reactions.x_enabled", False):
        return []
    return run.guarded(
        lambda: run.clients.social.topics(run.date_str),
        target="xAI（Xの話題）",
        impact="「昨日Xで話題になったこと」が載りません",
        default=[]) or []


def summarize(result: SocialResult | None) -> str:
    """記事・ノートへ載せる文字列。出典が0件なら定型文（AC-17）。

    個人の投稿本文とアカウント名は載せない。傾向の要約だけ（AC-18）。
    """
    if result is None:
        return social_parser.NO_REACTION
    return social_parser.summary_text(result)


def free_summary(article: Article) -> str:
    """無料の反応から傾向の1行を作る。純粋関数。"""
    r = article.reactions
    parts = []
    if r.hn_points:
        parts.append(f"Hacker Newsで{r.hn_points}点・{r.hn_comments}コメント")
    if r.hatena:
        parts.append(f"はてなブックマーク{r.hatena}件")
    if r.reddit:
        parts.append(f"Redditスコア{r.reddit}")
    if not parts:
        return social_parser.NO_REACTION
    return "、".join(parts) + "の反応があった"
