"""RSS / Atom / RDF の正規化。Mock と Real が同じこの関数を通る。

入力は生のXMLバイト列。ネットワークもファイルも触らない純粋関数。

対応する3つの形式:

- **RSS 2.0** —— `<rss><channel><item>`。名前空間なし
- **Atom** —— `<feed><entry>`。`http://www.w3.org/2005/Atom`
- **RSS 1.0 (RDF)** —— `<rdf:RDF><item>`。既定名前空間が
  `http://purl.org/rss/1.0/` で、`<item>` は `<channel>` の外に並ぶ。
  日付は `dc:date`。**日本のサイトで広く使われている**（PC Watch、窓の杜、
  JPCERT/CC など）ので、落とすと情報源がまとまって欠ける。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from ...models import Article
from . import article_html

_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_TAG_RE = re.compile(r"<[^>]+>")

JST = timezone(timedelta(hours=9))


def _published_at(raw: str) -> str:
    """フィードの日時を Vault 規約の ISO 8601（JST）へ正規化する。

    RSS 2.0 は RFC 822（`Mon, 31 Aug 2026 23:00:00 +0000`）、Atom と RDF は
    ISO 8601 と、形式がばらばら。そのまま入れると並び替えも比較もできない。
    """
    value = (raw or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    else:
        parsed = parsed.astimezone(JST)
    return parsed.isoformat(timespec="seconds")


def _text(el, path: str, ns: dict | None = None) -> str:
    found = el.find(path, ns or _NS)
    if found is None:
        return ""
    return (found.text or "").strip()


def _first_text(el, *paths: str) -> str:
    """複数の書き方を順に試す。形式ごとに要素名と名前空間が違うため。"""
    for path in paths:
        value = _text(el, path)
        if value:
            return value
    return ""


def _body_of(html: str) -> str:
    """全文の HTML を Markdown へ。段落と文字参照を保つ。

    タグを正規表現で落とすだけだと段落が潰れ、`&amp;` がそのまま残る。
    記事HTMLと同じ `article_html.to_markdown` を通す。
    """
    body, _ = article_html.to_markdown(html or "")
    return body


def _find_items(root) -> list:
    """フィードの項目を集める。3形式のどれでも拾う。"""
    for path in (".//item",              # RSS 2.0
                 ".//rss1:item",         # RSS 1.0 (RDF)
                 ".//atom:entry"):       # Atom
        found = root.findall(path, _NS)
        if found:
            return found
    return []


def _link_of(item) -> str:
    link = _first_text(item, "link", "rss1:link")
    if link:
        return link

    # Atom はテキストではなく href 属性に入る
    for path in ("atom:link[@rel='alternate']", "atom:link", "link"):
        element = item.find(path, _NS)
        if element is not None:
            href = (element.get("href") or "").strip()
            if href:
                return href

    # YouTubeチャンネルRSSは動画IDだけを持つ
    video_id = _text(item, "yt:videoId")
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return ""


def _published_of(item) -> str:
    return _published_at(_first_text(
        item, "pubDate", "dc:date", "atom:published", "atom:updated",
        "rss1:date"))


_REDDIT_PREVIEW = re.compile(r"https://preview\.redd\.it/([a-z0-9]+)\.(jpg|jpeg|png|gif)", re.I)
_IMG_SRC = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)


def _image_of(item) -> str:
    """項目の画像URL。`media:thumbnail` / `media:content` / 本文の最初の `<img>`。

    Reddit のサムネイルは 140px の切り抜きで署名付き（`preview.redd.it`）。
    同じ画像の原寸が `i.redd.it/<id>.<ext>` にあり署名なしで取れるので、
    そちらへ読み替える。
    """
    candidates: list[str] = []
    for path in ("media:thumbnail", "media:content"):
        for el in item.findall(path, _NS):
            url = (el.get("url") or "").strip()
            if url:
                candidates.append(url)
    for path in ("content:encoded", "atom:content", "description"):
        el = item.find(path, _NS)
        if el is not None:
            inner = "".join(el.itertext())
            candidates.extend(_IMG_SRC.findall(inner))
    for url in candidates:
        url = url.replace("&amp;", "&")
        m = _REDDIT_PREVIEW.match(url)
        if m:
            return f"https://i.redd.it/{m.group(1)}.{m.group(2)}"
        if url.startswith("https://"):
            return url
    return ""


def _fulltext_of(item) -> str:
    """フィードが本文を全文配信していれば取り出す。"""
    encoded = _text(item, "content:encoded")
    if encoded:
        return _body_of(encoded)

    # Atom は content 要素に入る
    element = item.find("atom:content", _NS)
    if element is not None:
        inner = "".join(element.itertext()).strip()
        if inner:
            return _body_of(inner)
    return ""


def parse(xml_bytes: bytes, source_id: str, default_category: str,
          fulltext: bool = False, origin: str = "config",
          kind: str = "article") -> list[Article]:
    """フィードを Article のリストへ正規化する。壊れたXMLは空リストを返す。

    `kind="gallery"` は個人投稿（写真集）。本文の代わりに投稿文を短く持ち、
    画像URLを必ず拾う。画像の無い投稿は落とす。
    """
    if not xml_bytes or not xml_bytes.strip():
        return []
    try:
        # XML宣言の前に空白があると「宣言が先頭に無い」で落ちる（Akko の
        # WordPress フィードで発生）。先頭の空白は落としてから読む
        root = ET.fromstring(xml_bytes.lstrip())
    except ET.ParseError:
        return []

    articles: list[Article] = []
    for item in _find_items(root):
        title = _first_text(item, "title", "rss1:title", "atom:title")
        link = _link_of(item)
        if not title or not link:
            continue  # 必須フィールド欠落は落とす（欠落注入で踏む経路）

        body = ""
        extracted_by = "none"
        image_url = _image_of(item)
        if kind == "gallery":
            if not image_url:
                continue
            body = _gallery_text(_raw_content_of(item))
            extracted_by = "rss"
        elif fulltext:
            body = _fulltext_of(item)
            if body:
                extracted_by = "rss"

        articles.append(Article(
            id="", url=link, title=title, source=source_id,
            category=default_category, published_at=_published_of(item),
            origin=origin, body_md=body, extracted_by=extracted_by,
            image_url=image_url if kind == "gallery" else "",
            kind=kind,
        ))
    return articles


_GALLERY_NOISE = re.compile(r"submitted by|\[link\]|\[comments\]|^\s*\|", re.I)


def _raw_content_of(item) -> str:
    """項目の本文HTMLをそのまま（Reddit は表の中に投稿文が入る）。"""
    for path in ("content:encoded", "atom:content", "description"):
        el = item.find(path, _NS)
        if el is not None:
            inner = "".join(el.itertext()).strip()
            if inner:
                return inner
    return ""


def _gallery_text(raw_html: str) -> str:
    """個人投稿の本文を素の文にし、投稿者名や定型のリンク行を落とす（AC-18）。

    記事用の `article_html.to_markdown` は表やリンクの塊をノイズとして落とす
    ので使えない。タグの境目で行に分け、定型の行だけ捨てる。
    """
    import html as html_lib
    text = html_lib.unescape(raw_html or "")
    text = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<(br|/p|/td|/tr|/div|/li)[^>]*>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    lines: list[str] = []
    for line in text.splitlines():
        # 「submitted by /u/…」以降は投稿者名と定型リンク。そこから先を切る
        line = re.split(r"(?i)submitted by", line, maxsplit=1)[0]
        line = " ".join(line.split())
        if line and not _GALLERY_NOISE.search(line):
            lines.append(line)
    return "\n".join(lines)[:600]
