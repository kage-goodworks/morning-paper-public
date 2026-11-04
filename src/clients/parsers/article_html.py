"""記事HTMLから本文だけをMarkdownへ落とす。純粋関数（AC-13 / AC-14）。

ナビゲーション・広告・サイドバー・フッターを落とす。整形はコード処理で行い、
LLMは使わない（FR-1）。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

DROP_TAGS = {"nav", "header", "footer", "aside", "script", "style", "form", "noscript"}
DROP_HINTS = ("ad", "advert", "promo", "sidebar", "nav", "related", "social",
              "share", "banner", "recommend", "newsletter", "cookie")
BLOCK_TAGS = {"p", "div", "section", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6"}
HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}


#: 本文の先頭に混ざる、文字サイズ設定の部品（Ars Technica）
_TEXT_SETTINGS = re.compile(r"\A\s*Text\s+settings\b.{0,400}?Minimize to nav\s*", re.S | re.I)
#: Jina Reader が本文の前に付ける見出し
_READER_HEAD = re.compile(r"\A\s*Title:.{0,600}?URL Source:.{0,1500}?Markdown Content:\s*", re.S)
#: 取得先に断られたときの Jina Reader の文言と、確認ページの題名
_REFUSED = re.compile(
    r"Warning: Target URL returned error|^Title: *(?:Page Not Found|404|403|"
    r"Just a moment|Access Denied|Security check|Attention Required)"
    r"|^#* *Prove your humanity", re.M | re.I)
#: これ以上の割合で文字化けしていたら本文として使わない
MAX_BROKEN_RATIO = 0.02


_MD_LINK = re.compile(r"\]\((https?://[^)\s]+)")


def markdown_links(markdown: str) -> list[str]:
    """Markdown の中のリンクを、重複を除いて出てきた順に返す。純粋関数。"""
    return list(dict.fromkeys(_MD_LINK.findall(markdown or "")))


def drop_markdown_links(markdown: str) -> str:
    """Markdown のリンクを文字だけに戻す。純粋関数。"""
    return re.sub(r"\[([^\]]*)\]\(https?://[^)\s]*\)", r"\1", markdown or "")


def strip_chrome(body: str) -> str:
    """本文の先頭に残ったサイトの部品を外す。純粋関数。"""
    return _TEXT_SETTINGS.sub("", _READER_HEAD.sub("", body or "", count=1), count=1)


def usable_body(raw: str) -> bool:
    """取得した文字が本文として使えるか。エラーページと文字化けは落とす。純粋関数。"""
    if _REFUSED.search((raw or "")[:800]):
        return False
    text = strip_chrome(raw).strip()
    return bool(text) and text.count("\ufffd") / len(text) <= MAX_BROKEN_RATIO


def _is_noise(attrs: list[tuple[str, str | None]]) -> bool:
    blob = " ".join((v or "").lower() for k, v in attrs if k in ("class", "id", "role"))
    if not blob:
        return False
    # `ad` の部分一致だと `lead` や `roadmap` まで広告として落ちる。
    tokens = [t for t in re.split(r"[^a-z0-9]+", blob) if t]
    return any(token == "ad" or any(
        token.startswith(hint) for hint in DROP_HINTS if hint != "ad")
        for token in tokens)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._drop_depth = 0
        self._drop_tag: str | None = None
        self._heading: str | None = None
        self._in_main = False
        self._main_depth = 0

    def handle_starttag(self, tag, attrs):
        if self._drop_depth:
            self._drop_depth += 1
            return
        if tag in DROP_TAGS or _is_noise(attrs):
            self._drop_depth = 1
            self._drop_tag = tag
            return
        if tag in ("article", "main"):
            self._in_main = True
            self._main_depth = 1
        elif self._in_main:
            self._main_depth += 1
        if tag in HEADING_TAGS:
            self._heading = HEADING_TAGS[tag]
            self.parts.append("\n\n" + self._heading + " ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith("http"):
                self.links.append(href)

    def handle_endtag(self, tag):
        if self._drop_depth:
            self._drop_depth -= 1
            if self._drop_depth == 0:
                self._drop_tag = None
            return
        if tag in HEADING_TAGS:
            self._heading = None
        if self._in_main:
            self._main_depth -= 1
            if self._main_depth <= 0 and tag in ("article", "main"):
                self._in_main = False

    def handle_data(self, data):
        if self._drop_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")


_META_IMAGE = re.compile(
    r"""<meta\s+[^>]*?(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["'][^>]*?content\s*=\s*["']([^"']+)["']"""
    r"""|<meta\s+[^>]*?content\s*=\s*["']([^"']+)["'][^>]*?(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["']""",
    re.I)


def representative_image(html: str) -> str:
    """記事の代表画像のURL。`og:image`（無ければ `twitter:image`）。純粋関数。

    ニュースサイトは SNS 共有用にほぼ必ず持っている。属性の順序が
    content が先でも property が先でも拾う。https 以外は使わない。
    """
    for m in _META_IMAGE.finditer(html or ""):
        url = (m.group(1) or m.group(2) or "").strip()
        url = url.replace("&amp;", "&")
        if url.startswith("https://"):
            return url
    return ""


def to_markdown(html: str) -> tuple[str, list[str]]:
    """本文Markdownと、本文中の外部リンクを返す。

    `<article>` か `<main>` があればその中だけを対象にする。無ければ全体から
    ノイズを落として拾う。
    """
    if not html or not html.strip():
        return "", []

    scoped = html
    m = re.search(r"<(article|main)\b.*?>(.*?)</\1>", html, re.S | re.I)
    if m:
        scoped = m.group(2)

    p = _Extractor()
    p.feed(scoped)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    seen: set[str] = set()
    links: list[str] = []
    for href in p.links:
        if href not in seen:
            seen.add(href)
            links.append(href)
    return text, links
