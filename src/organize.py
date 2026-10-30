"""整理 —— 重複排除・カテゴリ分け・話題への統合・上限と繰り越し。

**外部I/Oを持たない純粋関数だけを置く（NFR-1）。**LLMも使わない。
順位付けとスコアリングは行わない（FR-3）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Article, Story

#: 追跡用のクエリパラメータ。除去しても同じページを指す。
TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src",
                 "spm", "yclid", "msclkid"}

_AMP_SUFFIX = re.compile(r"(/amp/?|\.amp(\.html?)?)$", re.I)
_AMP_PREFIX = re.compile(r"^/amp/", re.I)

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[　-〿！-｠!-/:-@\[-`{-~]")


# --------------------------------------------------------------------------
# URL とタイトルの正規化
# --------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """追跡パラメータを外し、AMPを解決する（AC-1）。"""
    if not url:
        return url
    parts = urlsplit(url.strip())

    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_KEYS
    ]

    path = parts.path
    path = _AMP_SUFFIX.sub("", path)
    path = _AMP_PREFIX.sub("/", path)
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    return urlunsplit((parts.scheme.lower(), netloc, path,
                       urlencode(query), ""))


def normalize_title(title: str) -> str:
    """比較用にタイトルを正規化する。記号と空白の差を吸収する。"""
    t = (title or "").strip().lower()
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


# --------------------------------------------------------------------------
# 重複排除
# --------------------------------------------------------------------------

JST = timezone(timedelta(hours=9))

#: 前回成功時点はこの時刻で記録される（main.py と揃える）
SUCCESS_HOUR = "05:00:00+09:00"


def _parse_when(value: str) -> datetime | None:
    """ISO 8601 を aware な datetime へ。読めなければ None。無い日時は判断しない。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed


def default_window_start(date_str: str) -> str:
    """初回の対象期間の始まり。「直近24時間」= 前日の 05:00 JST。

    `last_success_at` が `<日付>T05:00:00+09:00` で記録されるのに合わせ、
    2回目以降と同じ物差しで24時間さかのぼる。
    """
    day = date.fromisoformat(date_str) - timedelta(days=1)
    return f"{day.isoformat()}T{SUCCESS_HOUR}"


def build_galleries(items: list[Article], *, today: str,
                    labels: dict[str, str], per_gallery: int = 6
                    ) -> tuple[list[Story], list[Article]]:
    """個人投稿をカテゴリごとに1つの「写真集」の話題へまとめる。純粋関数。

    新しい順に `per_gallery` 件まで。一覧には1行だけ出て、記事化すると
    1ページに写真が並ぶ。戻り値は (写真集の話題, 採用した投稿)。
    """
    by_category: dict[str, list[Article]] = {}
    for item in items:
        if item.kind == "gallery" and item.image_url:
            by_category.setdefault(item.category, []).append(item)
    stories: list[Story] = []
    chosen: list[Article] = []
    for category, group in by_category.items():
        picked = sort_newest_first(group)[:per_gallery]
        if not picked:
            continue
        label = labels.get(category, category)
        stories.append(Story(
            id="g_" + story_id(f"gallery:{today}:{category}")[2:],
            title=f"🖼 {label}の写真集",
            category=category, keywords=[],
            article_ids=[a.id for a in picked],
            first_seen=today, last_updated=today,
            latest_url=picked[0].url, kind="gallery"))
        chosen.extend(picked)
    return stories, chosen


def success_point(collected_at: str, unsaved: list[Article]) -> str:
    """次回の対象期間の始まり。純粋関数。

    保存できなかった記事があれば、その中で最も古い公開時刻より先へは
    進めない（次回の対象期間に入るように）。公開時刻が読めない記事が
    あれば安全側に倒して進めない（`collected_at` を返さず、空を返す
    のではなく、呼び出し側が前回の値を保つよう最古の値も無いときは
    `collected_at` より前の値として "" を返す）。
    """
    if not unsaved:
        return collected_at
    stamps = [_parse_when(a.published_at) for a in unsaved]
    if any(s is None for s in stamps):
        return ""          # 判断できない。進めない
    oldest = min(stamps)
    limit = _parse_when(collected_at)
    if limit is None or oldest >= limit:
        return collected_at
    return oldest.isoformat(timespec="seconds")


#: 落ちていたソースが復旧したとき、さかのぼって拾う日数の上限。
#: 何十日も止まっていたソースの全記事が一度に流れ込むのを防ぐ
MAX_BACKFILL_DAYS = 3


def within_window(articles: list[Article], since: str, *,
                  per_source: dict[str, str] | None = None
                  ) -> tuple[list[Article], int]:
    """対象期間（`since` 以降）に絞る（Requirements「前回成功時点以降」）。

    フィードは過去の記事を数百〜千件単位で載せていることがある。ここで
    絞らないと、初回に全件を「新着」として本文取得へ回してしまい、15分の
    枠に収まらない。日時のない記事は判断できないので残す（seen が次回
    以降を防ぐ）。戻り値は (残った記事, 落とした件数)。

    `per_source` はソースごとの前回成功時点。落ちていたソースは全体より
    古い時点を持つので、復旧後にその間の記事も拾える。ただし
    `MAX_BACKFILL_DAYS` より前へはさかのぼらない。
    """
    floor = _parse_when(since)
    if floor is None:
        return list(articles), 0
    backfill_floor = floor - timedelta(days=MAX_BACKFILL_DAYS)
    kept: list[Article] = []
    dropped = 0
    for article in articles:
        when = _parse_when(article.published_at)
        limit = floor
        own = _parse_when((per_source or {}).get(article.source, ""))
        if own is not None and own < floor:
            limit = max(own, backfill_floor)
        if when is None or when >= limit:
            kept.append(article)
        else:
            dropped += 1
    return kept, dropped


def dedupe(articles: list[Article], *, seen: dict[str, str] | None = None,
           title_norm: dict[str, str] | None = None,
           similarity: float = 0.85) -> tuple[list[Article], int]:
    """重複を統合し、`seen` に含まれるものを除外する（AC-1 / AC-12）。

    戻り値は (残った記事, 落とした件数)。
    """
    seen = seen or {}
    title_norm = title_norm or {}

    kept: list[Article] = []
    dropped = 0
    seen_hashes: set[str] = set()
    kept_titles: list[str] = []

    for article in articles:
        article.url = normalize_url(article.url)
        article.url_hash = hashlib.sha256(
            article.url.encode("utf-8")).hexdigest()[:16]
        article.id = article.url_hash[:8]

        if article.url_hash in seen_hashes or article.url_hash in seen:
            dropped += 1
            continue

        norm = normalize_title(article.title)
        if norm and norm in title_norm:
            dropped += 1
            continue

        duplicate = False
        for existing in kept_titles:
            if title_similarity(article.title, existing) >= similarity:
                duplicate = True
                break
        if duplicate:
            dropped += 1
            continue

        seen_hashes.add(article.url_hash)
        kept_titles.append(article.title)
        kept.append(article)

    return kept, dropped


# --------------------------------------------------------------------------
# カテゴリ
# --------------------------------------------------------------------------

_ASCII_WORD = re.compile(r"\A[A-Za-z0-9+#.\-]+\Z")


def keyword_hits(title: str, word: str) -> bool:
    """キーワードが見出しに含まれるか。

    英数字のキーワードは**単語として**一致したときだけ。部分一致だと
    `Rust` が「trust」、`Go` が「Google」、`CI` が「decision」に当たり、
    宇宙飛行士の記事が「言語・フレームワーク」に入る（本番で発生）。
    日本語は語境界が無いので部分一致のまま。
    """
    if not word or not title:
        return False
    if _ASCII_WORD.match(word):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])"
        return re.search(pattern, title, re.IGNORECASE) is not None
    return word.lower() in title.lower()


def categorize(article: Article, keyword_map: dict[str, list[str]]) -> str:
    """見出しのキーワードでソース既定のカテゴリを上書きする（FR-3）。"""
    title = article.title or ""
    for category, words in (keyword_map or {}).items():
        for word in words:
            if keyword_hits(title, word):
                return category
    return article.category


def group_by_category(articles_or_stories: Iterable[Any],
                      order: list[str]) -> dict[str, list[Any]]:
    """カテゴリごとにまとめ、`order` の並びで返す（AC-2）。"""
    buckets: dict[str, list[Any]] = {c: [] for c in order}
    for item in articles_or_stories:
        buckets.setdefault(item.category, []).append(item)
    return {c: buckets[c] for c in order if buckets.get(c)}


def sort_newest_first(items: list[Any]) -> list[Any]:
    """新しい順に並べる。順位付けではなく時刻順（FR-3）。"""
    def key(item: Any) -> str:
        value = getattr(item, "published_at", None) or getattr(item, "last_updated", "")
        return value or ""
    return sorted(items, key=key, reverse=True)


# --------------------------------------------------------------------------
# 固有名詞の抽出
# --------------------------------------------------------------------------

_PROPER = re.compile(
    r"[A-Z][A-Za-z0-9+#.-]{1,}"          # 英語の大文字始まり
    r"|[゠-ヿー]{3,}"         # カタカナ語
    r"|v?\d+\.\d+(?:\.\d+)?"              # バージョン番号
    r"|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"   # owner/repo
)


def extract_keywords(title: str, body_head: str = "",
                     stopwords: Iterable[str] | None = None) -> list[str]:
    """タイトルと本文冒頭から固有名詞を拾う。LLMは使わない。"""
    stop = {s.lower() for s in (stopwords or [])}
    text = (title or "") + " " + (body_head or "")[:300]
    out: list[str] = []
    seen: set[str] = set()
    for match in _PROPER.findall(text):
        token = match.strip(".-")
        if len(token) < 2 or token.lower() in stop:
            continue
        key = token.lower()
        if key not in seen:
            seen.add(key)
            out.append(token)
    return out


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa = {x.lower() for x in a}
    sb = {x.lower() for x in b}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# --------------------------------------------------------------------------
# 話題への統合
# --------------------------------------------------------------------------

def story_id(seed: str) -> str:
    return "s_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6]


def _matches(article: Article, story: Story, article_urls: dict[str, list[str]],
             overlap: float, stopwords: Iterable[str]) -> bool:
    """統合の判定。カテゴリ一致が前提条件（AC-8）。"""
    if article.category != story.category:
        return False

    keywords = extract_keywords(article.title, article.body_md, stopwords)
    if jaccard(keywords, story.keywords) >= overlap:
        return True

    # 本文中の外部リンクが、その話題の記事URLを指している（AC-10）
    known = {normalize_url(u) for u in article_urls.get(story.id, [])}
    for link in article.outbound_links:
        if normalize_url(link) in known:
            return True
    return False


def build_stories(articles: list[Article], carryover: list[Story], *,
                  today: str, overlap: float = 0.5,
                  stopwords: Iterable[str] | None = None,
                  article_urls: dict[str, list[str]] | None = None,
                  ) -> tuple[list[Story], int]:
    """新着記事を既存の話題へ統合するか、新しい話題を作る。

    照合の対象は `status == "carryover"` の話題だけ。一度一覧に出した話題
    （`listed` / `written`）は後から見え方を変えないため対象外にする（AC-9）。

    戻り値は (話題のリスト, 統合が起きた件数)。
    """
    stopwords = list(stopwords or [])
    article_urls = dict(article_urls or {})

    stories = [Story(**s.to_dict()) for s in carryover]
    # 照合の対象は「繰り越された話題」と「その日の新着」の間だけ。その日に
    # 新しく作った話題は候補へ入れない（FR-3「誤統合を避けるための制限」）。
    candidates = [s for s in stories if s.status == "carryover"]
    merged = 0

    for article in articles:
        target: Story | None = None
        for story in candidates:
            if _matches(article, story, article_urls, overlap, stopwords):
                target = story
                break

        keywords = extract_keywords(article.title, article.body_md, stopwords)

        if target is not None:
            if article.id not in target.article_ids:
                target.article_ids.append(article.id)
            target.title = article.title_short or article.title
            target.latest_url = article.url
            target.keywords = sorted(set(target.keywords) | set(keywords))
            target.last_updated = today
            article_urls.setdefault(target.id, []).append(article.url)
            merged += 1
            continue

        new_story = Story(
            id=story_id(article.url), title=article.title_short or article.title,
            category=article.category, keywords=keywords,
            article_ids=[article.id], first_seen=today, last_updated=today,
            status="carryover", latest_url=article.url,
        )
        stories.append(new_story)
        article_urls.setdefault(new_story.id, []).append(article.url)

    return stories, merged


def expire(stories: list[Story], *, today: str, carryover_days: int = 7
           ) -> tuple[list[Story], int]:
    """`first_seen` から期限を過ぎた未表示の話題を落とす（AC-11）。

    記事そのものはVaultの日次ノートに残るため、消えるのは一覧の候補からだけ。
    """
    try:
        today_d = date.fromisoformat(today)
    except ValueError:
        return stories, 0

    kept: list[Story] = []
    removed = 0
    for story in stories:
        if story.status != "carryover":
            kept.append(story)
            continue
        try:
            first = date.fromisoformat(story.first_seen)
        except ValueError:
            kept.append(story)
            continue
        if (today_d - first) > timedelta(days=carryover_days):
            removed += 1
            continue
        kept.append(story)
    return kept, removed


def apply_limits(stories: list[Story], *, order: list[str], per_category: int
                 ) -> tuple[dict[str, list[Story]], dict[str, tuple[int, int]]]:
    """カテゴリごとに新しい順で上限まで切る。順位付けはしない（FR-3）。

    戻り値は (表示する話題, カテゴリ -> (総数, 表示数))。
    溢れた話題は呼び出し側が `carryover` のまま残す。
    """
    grouped = group_by_category(stories, order)
    shown: dict[str, list[Story]] = {}
    counts: dict[str, tuple[int, int]] = {}

    for category, items in grouped.items():
        ordered = sort_newest_first(items)
        keep = ordered[:per_category]
        shown[category] = keep
        counts[category] = (len(ordered), len(keep))
    return shown, counts


def prune_seen(seen: dict[str, str], *, today: str, days: int = 30) -> dict[str, str]:
    """`seen` を刈り取る。既定30日（Spec の state.json）。"""
    try:
        today_d = date.fromisoformat(today)
    except ValueError:
        return seen
    out: dict[str, str] = {}
    for key, value in seen.items():
        try:
            when = date.fromisoformat(value)
        except ValueError:
            continue
        if (today_d - when) <= timedelta(days=days):
            out[key] = value
    return out
