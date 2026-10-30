"""収集（副作用あり）。

`config.yml` と `youtube.yml` の全ソースを取得する。ソース単位で失敗しても
そのソースを飛ばすだけで、処理は止めない（FR-1）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .models import Article
from .pipeline import Run

TIMEOUT_SECONDS = 20

#: GitHub Releases の記事に付くソースid（parsers/releases.py と揃える）
RELEASES_SOURCE = "github_releases"


def collect(run: Run) -> list[Article]:
    """全ソースから記事を集める。失敗したソースは飛ばす。"""
    sources = list(run.config.get("sources", []) or [])
    for channel in run.config.get("youtube", []) or []:
        sources.append({**channel, "type": "youtube",
                        "category": channel.get("category", "ai")})

    articles: list[Article] = []
    skipped: list[str] = []

    def fetch(source: dict) -> list[Article]:
        return run.guarded(
            lambda: run.clients.feed.fetch_articles(source),
            target=f"{source.get('id')}（フィード取得）",
            impact="今日の一覧にこの情報源の記事が載りません",
            default=None,
            status=None,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, sources))

    for source, result in zip(sources, results):
        if result is None:
            skipped.append(str(source.get("id")))
            continue
        articles.extend(result)

    if skipped:
        run.note_status(
            f"収集: {len(skipped)}件のソースを取得できませんでした"
            f"（{', '.join(skipped)}）")
    run.failed_sources = list(skipped)

    return articles


def collect_releases(run: Run) -> list[Article]:
    """GitHub Releases。探索の段階3でも使う。"""
    out: list[Article] = []
    failed = False
    for repo in run.config.get("releases.repos", []) or []:
        result = run.guarded(
            lambda repo=repo: run.clients.releases.fetch(repo),
            target=f"GitHub Releases（{repo}）",
            impact="そのリポジトリのリリースが一覧に載りません",
            default=None)
        if result is None:
            failed = True
            continue
        out.extend(result)
    # リリースは1つのソース（source="github_releases"）として扱う。1つでも
    # 落ちたら、そのソースの前回成功時点は進めない（復旧後に拾い直す）
    if failed and RELEASES_SOURCE not in run.failed_sources:
        run.failed_sources.append(RELEASES_SOURCE)
    return out


def collect_quotes(run: Run):
    """株価。落ちても一覧は成立する（縮退）。"""
    symbols = list(run.config.get("quotes.symbols", []) or [])
    return run.guarded(
        lambda: run.clients.quotes.fetch(symbols),
        target="Yahoo Finance（株価の取得）",
        impact="今日の一覧に株価が載りません",
        default=[],
        status="株価: 取得失敗（3回リトライ後）") or []
