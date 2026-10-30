"""Vaultノートの組み立てと読み出し。

**外部I/Oを持たない純粋関数（NFR-1）。**書き込むのは `src/vault.py`。

Vaultに残すのは「取得した記事そのもの」だけ。AIが加工したもの（Claudeの解説、
紙面画像）は保存しない。元データがあれば作り直せるため。

保存先は3種類。

| ノート | 単位 | 中身 |
|---|---|---|
| 記事ノート | 1記事（1URL） | URL・メタ情報・整形済みMarkdown全文 |
| デイリーノート | 1日 | 題名とURLの一覧、取得状況 |
| 未発行 | 1ファイル | 上限を超えて一覧に出せなかった話題へのリンク集 |

**一度書いた記事ノートは書き換えない。**新しい情報は新しいノートにする。
書き換えを伴う処理は失敗したときに元へ戻せないため。
"""

from __future__ import annotations

import re
from typing import Any

from . import safety
from .models import Article, Quote, Reactions, Story

# --- デイリーノートのセクション名 -------------------------------------------
SECTION_LIST = "一覧"
SECTION_PENDING = "未発行"
SECTION_X = "X上の話題"
SECTION_QUOTES = "株価"
SECTION_STATUS = "取得状況"

# --- 記事ノートのセクション名 -----------------------------------------------
SECTION_BODY = "本文"
SECTION_REACTIONS = "反応"

#: カテゴリから [[AI_GUIDE]] の tags 列挙値への対応。
#: **列挙値を勝手に増やさない**（AI_GUIDE「機械処理ではプロパティ名と列挙値を
#: 変更しない」）。細かい区分は `category` キーが持つ。
CATEGORY_TOPIC = {
    "ai": "topic/ai",
    "lang": "topic/development",
    "devtools": "topic/development",
    "infra": "topic/development",
    "industry": "topic/development",
    "security": "topic/development",
    "gadget": "topic/development",
    "keyboard": "topic/development",
    "career": "topic/development",
}

_HEADING_RE = re.compile(r"^(#{1,4})\s+", re.M)


def demote_headings(body: str) -> str:
    """本文中の見出しを2段下げる。

    記事側の `#` をそのまま埋めると、ノートの `## 本文` より上位になって
    構造が壊れる。`###` 以下へ落として、ノートの節の中に収める。
    """
    return _HEADING_RE.sub(lambda m: "#" * min(6, len(m.group(1)) + 2) + " ", body or "")


ARTICLE_DIR = "40_Knowledge/News"
DAILY_DIR = "50_Logs/News"


# --------------------------------------------------------------------------
# パス
# --------------------------------------------------------------------------

def article_path(article: Article) -> str:
    """記事ノートの置き場所。

    カテゴリ配下を年/月で切る。1フォルダあたりの件数を抑えるため。
    ファイル名は日付と記事IDにする。タイトルは日本語でファイル名に使えない
    （Vaultの規約が英小文字とハイフン）ため。
    """
    # 日時・カテゴリ・記事idはいずれも外部由来。`..` や `/` を含む値を
    # そのまま連結すると、Vaultの想定外の場所へ書き込める。
    date_str = safety.safe_date(article.published_at)
    category = safety.safe_segment(article.category, allowed=set(CATEGORY_TOPIC))
    article_id = safety.safe_id(article.id)
    year, month = (date_str[:4], date_str[5:7])
    return f"{ARTICLE_DIR}/{category}/{year}/{month}/{date_str}-{article_id}.md"


def daily_path(date_str: str) -> str:
    return f"{DAILY_DIR}/{date_str}.md"


def pending_path() -> str:
    return f"{DAILY_DIR}/_pending.md"


def weekly_path(week_str: str) -> str:
    return f"40_Knowledge/News-Review/{week_str}.md"


# --------------------------------------------------------------------------
# 記事ノート
# --------------------------------------------------------------------------

def build_article(article: Article, *, story_id: str, date_str: str,
                  agents: list[str] | None = None) -> str:
    """記事ノートを組み立てる。

    `story_id` を持たせるのが要点。`state.json` から話題の登録が消えたあとも、
    「どの記事とどの記事が同じ話題だったか」をVault側だけで辿れる。
    """
    agents = agents or []
    agent_block = ("agents:\n" + "\n".join(f"  - {a}" for a in agents)
                   if agents else "agents: []")
    topic = CATEGORY_TOPIC.get(article.category, "topic/development")
    r = article.reactions

    front = [
        "---",
        f"id: {safety.safe_date(date_str).replace('-', '')}-"
        f"{safety.safe_id(article.id)}",
        "type: reference",
        "status: done",
        "vault: open",
        f"created: {date_str}",
        f"updated: {date_str}",
        "source: automation",
        agent_block,
        "tags:",
        f"  - {topic}",
        "  - workflow/automation",
        f"summary: {safety.yaml_scalar(article.title)}",
        f"url: {safety.yaml_scalar(article.url)}",
        f"story_id: {safety.yaml_scalar(story_id)}",
        f"category: {safety.safe_segment(article.category, allowed=set(CATEGORY_TOPIC))}",
        f"publisher: {safety.yaml_scalar(article.source)}",
        f"published_at: {safety.yaml_scalar(article.published_at)}",
        f"extracted_by: {safety.yaml_scalar(article.extracted_by)}",
        f"image_url: {safety.yaml_scalar(article.image_url)}",
        "reactions:",
        f"  hn_points: {r.hn_points}",
        f"  hn_comments: {r.hn_comments}",
        f"  hatena: {r.hatena}",
        f"  reddit: {r.reddit}",
        "---",
        "",
        f"# {article.title.strip()}",
        "",
        f"出典: {safety.yaml_scalar(article.url)}",
    ]
    if article.title_original:
        front.append(f"原題: {article.title_original}")

    out = ["\n".join(front), "", f"## {SECTION_BODY}", ""]
    body = demote_headings((article.body_md or "").strip())
    out.append(body if body else "（本文を取得できませんでした）")

    out.extend(["", f"## {SECTION_REACTIONS}", ""])
    lines = []
    if r.hn_points:
        lines.append(f"- Hacker News {r.hn_points}点 / {r.hn_comments}コメント")
    if r.hatena:
        lines.append(f"- はてなブックマーク {r.hatena}件")
    if r.reddit:
        lines.append(f"- Reddit スコア {r.reddit}")
    if r.x_summary:
        lines.append(f"- X: {r.x_summary}")
    out.append("\n".join(lines) if lines else "- 目立った反応は見つからなかった")
    out.append("")

    return "\n".join(out)


_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def parse_article(text: str) -> Article | None:
    """記事ノートを `Article` へ戻す。純粋関数。

    記事化のときは、朝に保存したこのノートを読んで執筆する。**外部へは
    取りに行かない**（FR-5）。
    """
    if not text:
        return None
    match = _FRONT_RE.match(text)
    if not match:
        return None

    front: dict[str, Any] = {}
    reactions: dict[str, int] = {}
    in_reactions = False
    for line in match.group(1).split("\n"):
        if not line.strip():
            continue
        if line.startswith("reactions:"):
            in_reactions = True
            continue
        if in_reactions:
            if line.startswith("  ") and ":" in line:
                key, _, value = line.strip().partition(":")
                try:
                    reactions[key.strip()] = int(value.strip())
                except ValueError:
                    pass
                continue
            in_reactions = False
        if line.startswith(" ") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = safety.yaml_unscalar(value)

    url = front.get("url")
    if not url:
        return None

    # 反応の節にある「- X: …」（X上の傾向の要約）。frontmatter には無い
    x_summary = None
    reactions_marker = f"\n## {SECTION_REACTIONS}\n"
    if reactions_marker in text:
        for line in text.split(reactions_marker, 1)[1].splitlines():
            if line.startswith("## "):
                break
            if line.startswith("- X: "):
                x_summary = line[len("- X: "):].strip() or None

    body = ""
    marker = f"\n## {SECTION_BODY}\n"
    if marker in text:
        rest = text.split(marker, 1)[1]
        body = rest.split(f"\n## {SECTION_REACTIONS}", 1)[0].strip()
        if body == "（本文を取得できませんでした）":
            body = ""

    article_id = (front.get("id") or "").split("-")[-1]
    return Article(
        id=article_id,
        url=url,
        title=front.get("summary") or "",
        source=front.get("publisher") or "",
        category=front.get("category") or "industry",
        published_at=front.get("published_at") or "",
        body_md=body,
        extracted_by=front.get("extracted_by") or "none",
        image_url=front.get("image_url") or "",
        reactions=Reactions.from_dict({**reactions, "x_summary": x_summary}),
    )


def story_id_of(text: str) -> str | None:
    """記事ノートから `story_id` だけを取り出す。"""
    match = re.search(r"^story_id:\s*(\S+)\s*$", text or "", re.M)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# デイリーノート
# --------------------------------------------------------------------------

def _frontmatter(*, date_str: str, articles: int, stories: int,
                 pending: int, agents: list[str]) -> str:
    agent_lines = "\n".join(f"  - {a}" for a in agents) if agents else "  []"
    return (
        "---\n"
        f"id: {date_str.replace('-', '')}-0500-news\n"
        "type: log\n"
        "status: done\n"
        "vault: open\n"
        f"created: {date_str}\n"
        f"updated: {date_str}\n"
        "source: automation\n"
        "agents:\n"
        f"{agent_lines}\n"
        "tags:\n"
        "  - topic/ai\n"
        "  - workflow/automation\n"
        f"summary: {date_str}のITニュース {articles}件"
        f"（話題 {stories}件、未発行 {pending}件）\n"
        "---\n"
    )


def _link(article: Article) -> str:
    """記事ノートへの wikilink。拡張子を外す。"""
    return "[[" + article_path(article)[:-3] + "|" + article.title + "]]"


def build_daily(*, date_str: str, shown: dict[str, list[Story]],
                labels: dict[str, str], articles_by_id: dict[str, Article],
                status: list[str], pending_count: int = 0,
                quotes: list[Quote] | None = None,
                x_topics: list[str] | None = None,
                agents: list[str] | None = None) -> str:
    """デイリーノートを組み立てる。

    題名とURLの一覧、そして記事ノートへのリンク。**反応の数値は持たない**
    （記事ノート側にあるため、ここに置くと同じ情報が2か所になる）。
    """
    total_articles = len({a for group in shown.values() for s in group
                          for a in s.article_ids})
    total_stories = sum(len(v) for v in shown.values())

    out: list[str] = [
        _frontmatter(date_str=date_str, articles=total_articles,
                     stories=total_stories, pending=pending_count,
                     agents=agents or []),
        f"\n# {date_str} のニュース\n",
        f"\n## {SECTION_LIST}\n",
    ]

    for category, stories in shown.items():
        if not stories:
            continue
        out.append(f"\n### {labels.get(category, category)}\n")
        for story in stories:
            out.append(f"\n#### {story.title} ^{story.id}\n")
            for article_id in story.article_ids:
                article = articles_by_id.get(article_id)
                if article is None:
                    continue
                out.append(f"- [{article.title}]({article.url}) — "
                           f"{_link(article)}\n")

    if pending_count:
        out.append(f"\n## {SECTION_PENDING}\n\n")
        out.append(f"上限を超えて一覧に出せなかった話題が{pending_count}件あります。"
                   f"[[{pending_path()[:-3]}|未発行の一覧]]\n")

    if x_topics:
        out.append(f"\n## {SECTION_X}\n")
        for topic in x_topics:
            out.append(f"- {topic}\n")

    if quotes:
        out.append(f"\n## {SECTION_QUOTES}\n\n")
        out.append("| 銘柄 | 終値 | 前日比 |\n|---|---|---|\n")
        for q in quotes:
            out.append(f"| {q.symbol} | {q.close:,.2f} | {q.change_pct:+.2f}% |\n")

    out.append(f"\n## {SECTION_STATUS}\n")
    if status:
        for line in status:
            out.append(f"- {line}\n")
    else:
        out.append("- すべて取得できました\n")

    return "".join(out)


# --------------------------------------------------------------------------
# 未発行
# --------------------------------------------------------------------------

def build_pending(*, date_str: str, stories: list[Story],
                  labels: dict[str, str],
                  articles_by_id: dict[str, Article]) -> str:
    """未発行のリンク集。

    入るのは**上限を超えて一覧に出せなかった話題だけ**。一覧に出したが
    記事化されなかったものは消化済みとして扱い、ここへは入れない。

    毎朝 `state.json` から作り直す。処理済みは自動で消える。
    """
    out: list[str] = [
        "---\n"
        "id: news-pending\n"
        "type: log\n"
        "status: active\n"
        "vault: open\n"
        f"created: {date_str}\n"
        f"updated: {date_str}\n"
        "source: automation\n"
        "agents:\n  []\n"
        "tags:\n  - workflow/automation\n"
        f"summary: まだ一覧に出していない話題 {len(stories)}件\n"
        "---\n",
        "\n# 未発行\n",
        "\n上限を超えて一覧に出せなかった話題。翌日以降の候補として繰り越す。\n"
        "毎朝の実行で作り直すため、手で編集しても上書きされる。\n",
    ]

    if not stories:
        out.append("\n未発行の話題はありません。\n")
        return "".join(out)

    from . import organize
    grouped = organize.group_by_category(stories, list(labels.keys()))
    for category, group in grouped.items():
        out.append(f"\n## {labels.get(category, category)}\n\n")
        for story in group:
            links = []
            for article_id in story.article_ids:
                article = articles_by_id.get(article_id)
                if article is not None:
                    links.append(_link(article))
            suffix = f"（{story.first_seen} から繰り越し / 記事{len(story.article_ids)}件）"
            out.append(f"- {story.title} {suffix}\n")
            for link in links:
                out.append(f"    - {link}\n")

    return "".join(out)


# --------------------------------------------------------------------------
# 週次レビュー
# --------------------------------------------------------------------------

def build_weekly(*, week_str: str, entries: list[dict[str, Any]],
                 status: list[str] | None = None) -> str:
    """週次レビューのノート。日次ノートの該当日へリンクする。"""
    out: list[str] = [
        "---\n"
        f"id: {week_str.replace('-', '')}-news-review\n"
        "type: reference\n"
        "status: done\n"
        "vault: open\n"
        f"created: {week_str}\n"
        f"updated: {week_str}\n"
        "source: automation\n"
        "agents:\n  - claude\n"
        "tags:\n  - topic/ai\n  - workflow/analysis\n"
        f"summary: {week_str}の反応の変化 {len(entries)}件\n"
        "---\n",
        f"\n# {week_str} 反応の変化\n",
    ]
    for entry in entries:
        out.append(f"\n## {entry['title']}\n\n{entry['body'].strip()}\n")
        link = entry.get("daily_link")
        if link:
            out.append(f"\n元の記事: [[{link}]]\n")
    if status:
        out.append(f"\n## {SECTION_STATUS}\n")
        for line in status:
            out.append(f"- {line}\n")
    return "".join(out)
