"""Vaultへの書き込み（副作用あり）。

書き込む範囲は次の6種類だけ。この範囲の外へは書き込まない
（Overview「Vaultへ書き込む範囲」）。

| パス | 内容 |
|---|---|
| `50_Logs/News/YYYY-MM-DD.md` | デイリーノート |
| `50_Logs/News/_pending.md` | 未発行のリンク集 |
| `40_Knowledge/News/<cat>/<年>/<月>/<日付>-<id>.md` | 記事ノート |
| `40_Knowledge/News-Review/YYYY-Www.md` | 週次レビュー |
| `40_Knowledge/Tech/<cat>/<主題>.md` | 知識ノート（記事化した中身を主題ごとに追記。`knowledge.py`） |
| `40_Knowledge/Tech/_INDEX.md` | 知識の索引 |

**push前に必ず `guard.py` で検査する。**検出したら伏せ字にしてから push を
続行し、伏せ字にした位置を通知する。処理は止めない（NFR-2 / AC-22）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import guard, note
from .models import Article
from .pipeline import Run

daily_path = note.daily_path
weekly_path = note.weekly_path
pending_path = note.pending_path
article_path = note.article_path


@dataclass
class ArticleWrites:
    """記事ノートの保存結果。既存分も `available` に含める。"""
    created: int = 0
    available: set[str] = field(default_factory=set)


def _env_values() -> list[str]:
    """環境変数に載っている値そのもの。**ログにも通知にも出さない。**"""
    names = ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "ANTHROPIC_API_KEY",
             "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "XAI_API_KEY",
             "FIRECRAWL_API_KEY", "GH_DISPATCH_TOKEN", "VAULT_DEPLOY_KEY")
    return [v for v in (os.environ.get(n) for n in names) if v]


def scrub(run: Run, rel_path: str, content: str) -> str:
    """秘密を検査して伏せ字にする。処理は止めない。"""
    result = guard.scan(content, env_values=_env_values())
    if result.findings:
        run.note_status(guard.status_line(result.findings))
        run.reports.append(guard.build_report(rel_path, result.findings))
    return result.text


def write_note(run: Run, rel_path: str, content: str) -> bool:
    """1ファイル書く。commitはまとめて `commit` で行う。"""
    text = scrub(run, rel_path, content)
    ok = run.guarded(
        lambda: run.clients.notes.write(rel_path, text),
        target="Open Vault（ノートの保存）",
        impact="このノートの保存だけ翌日へ持ち越します",
        default=False, status=None)
    return ok is not False


def write_articles(run: Run, articles: list[Article],
                   story_of: dict[str, str], date_str: str) -> ArticleWrites:
    """記事ノートを書き出す。

    **一度書いたノートは書き換えない。**既にあるものは飛ばす。新しい情報は
    新しいノートになる（続報は別記事なので別ノート）。
    """
    result = ArticleWrites()
    for article in articles:
        rel = note.article_path(article)
        existing = run.guarded(
            lambda rel=rel: run.clients.notes.read(rel),
            target="Open Vault（ノートの確認）",
            impact="", default=None, status=None)
        if existing:
            result.available.add(article.id)
            continue
        agents = ["gemini"] if article.title_original else []
        text = note.build_article(article, story_id=story_of.get(article.id, ""),
                                  date_str=date_str, agents=agents)
        if write_note(run, rel, text):
            result.created += 1
            result.available.add(article.id)
    return result


def read_article(run: Run, rel_path: str) -> Article | None:
    """保存済みの記事ノートを読んで `Article` へ戻す。

    記事化はここから読む。**外部へは取りに行かない**（FR-5）。
    """
    text = run.guarded(
        lambda: run.clients.notes.read(rel_path),
        target="Open Vault（ノートの読み出し）",
        impact="その記事の本文を使えません",
        default=None, status=None)
    return note.parse_article(text) if text else None


def commit(run: Run, message: str) -> bool:
    """まとめてcommitしpushする。競合しても処理は止めない。"""
    ok = run.guarded(
        lambda: run.clients.notes.commit_push(message),
        target="Open Vault（commitとpush）",
        impact="Vaultへの保存だけ翌日へ持ち越します。Slackへの投稿は実行します",
        default=False,
        status="Vault保存: 失敗したため翌日へ持ち越します")
    return ok is not False
