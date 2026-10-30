"""紙面のHTML生成と、PDF / PNG の出力。

`build_html` は外部I/Oを持たない純粋関数でテスト対象（AC-15）。
`capture_pdf` と `capture` だけが副作用を持つ。

Slack へ送るのは **PDF 1つ**（2026-09-15 変更）。全ページが1ファイルに
まとまり、文字が選択・コピーでき、拡大しても鮮明で、出典のリンクが
埋め込める。PNG は目視確認用に残している。

**Playwrightはモックしない。**ローカルでChromiumを動かし、本物のPNGを作る。
紙面が本当に組めているかを目で確認するため。生成したHTMLは `file://` として
読み込み、Webサーバーを立てない（Spec「紙面」）。

画像生成AIは使わない。この工程にLLMは関与しない。
"""

from __future__ import annotations

import html as html_lib
import os
import re
from pathlib import Path

from . import safety
from .clients.parsers import article_sections
from .paginate import Page, PaperArticle

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"

WIDTH = 1240
HEIGHT = 1754
DEVICE_SCALE = 2


_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _table_to_html(lines: list[str]) -> str:
    """Markdown の表（`| a | b |`）を `<table>` へ。区切り行は捨てる。"""
    rows: list[list[str]] = []
    for line in lines:
        if _TABLE_SEP.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    out = ["<table>", "<thead><tr>"]
    out.extend(f"<th>{html_lib.escape(c)}</th>" for c in head)
    out.append("</tr></thead>")
    if body:
        out.append("<tbody>")
        for row in body:
            out.append("<tr>" + "".join(f"<td>{html_lib.escape(c)}</td>" for c in row) + "</tr>")
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


def _md_to_html(body: str) -> str:
    """記事本文の最小限のMarkdown変換。見出し・段落・箇条書き・表を扱う。"""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", (body or "").strip()):
        text = block.strip()
        if not text:
            continue
        lines = text.splitlines()
        heading = re.match(r"^(#{2,4})\s+(.*)$", text)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{html_lib.escape(heading.group(2))}</h{level}>")
            continue
        if all(line.strip().startswith("|") for line in lines):
            out.append(_table_to_html(lines))
            continue
        if all(re.match(r"^\s*[-*・]\s+", line) for line in lines):
            items = "".join(
                "<li>" + html_lib.escape(re.sub(r"^\s*[-*・]\s+", "", line)) + "</li>"
                for line in lines)
            out.append(f"<ul>{items}</ul>")
            continue
        out.append("<p>" + html_lib.escape(text).replace("\n", "<br>") + "</p>")
    return "\n".join(out)


def _link(url: str, text: str) -> str:
    """押せるリンク。取得先として許されないURLは文字だけにする。"""
    label = html_lib.escape(text)
    if url and safety.is_fetchable(url):
        return f'<a href="{html_lib.escape(url, quote=True)}">{label}</a>'
    return label


def _load(name: str) -> str:
    path = TEMPLATE_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def build_html(*, title: str, date_str: str, page: Page, total_pages: int,
               css: str | None = None, template: str | None = None,
               notice: str = "") -> str:
    """1ページ分の紙面HTMLを組み立てる。純粋関数（AC-15）。

    `css` と `template` を渡さない場合だけ `templates/` を読む。テストでは
    文字列を直接渡してI/Oなしで検証できる。
    """
    css = css if css is not None else _load("paper.css")
    template = template if template is not None else _load("paper.html")

    items: list[str] = []
    for article in page.articles:
        dates = ""
        if len(article.published_dates) > 1:
            dates = ('<div class="timeline">経過: '
                     + " → ".join(html_lib.escape(d)
                                  for d in article.published_dates)
                     + "</div>")
        sources = ""
        if article.sources:
            links = "<br>".join(_link(u, u) for u in article.sources)
            sources = f'<div class="sources">出典: {links}</div>'
        # 見出しは最初の出典へのリンクにする（PDF で押せる）
        first = article.sources[0] if article.sources else ""
        items.append(
            '<article class="story">'
            f'<h2>{_link(first, article.title)}</h2>'
            f"{dates}"
            f'<div class="body">{_md_to_html(article.body_md)}</div>'
            f"{sources}"
            "</article>"
        )

    notice_html = ""
    if notice:
        notice_html = ('<div class="notice">' + html_lib.escape(notice) + "</div>")

    return (template
            .replace("{{NOTICE}}", notice_html)
            .replace("{{CSS}}", css)
            .replace("{{TITLE}}", html_lib.escape(title))
            .replace("{{DATE}}", html_lib.escape(date_str))
            .replace("{{PAGE}}", str(page.index))
            .replace("{{TOTAL}}", str(total_pages))
            .replace("{{ARTICLES}}", "\n".join(items)))


#: PDF の各ページを紙面1枚ぶんの大きさにし、余白を持たせない
PDF_CSS = """
@page { size: 1240px 1754px; margin: 0; }
.sheet { page-break-after: always; }
.sheet:last-child { page-break-after: auto; }
a { color: inherit; text-decoration: none; }
"""


def build_pdf_html(pages: list[Page], *, title: str, date_str: str,
                   notice: str = "") -> str:
    """全ページを1つのHTMLにまとめる。純粋関数。

    各ページの `<div class="sheet">` を並べ、印刷用CSSで1ページ1紙面に
    割り付ける。
    """
    sheets: list[str] = []
    for page in pages:
        single = build_html(title=title, date_str=date_str, page=page,
                            total_pages=len(pages), notice=notice)
        start = single.index("<body>") + len("<body>")
        end = single.index("</body>")
        sheets.append(single[start:end].strip())
    head_end = pages and build_html(
        title=title, date_str=date_str, page=pages[0],
        total_pages=len(pages), notice=notice).index("</head>")
    head = build_html(title=title, date_str=date_str, page=pages[0],
                      total_pages=len(pages), notice=notice)[:head_end] if pages else ""
    head = head.replace("</style>", PDF_CSS + "</style>", 1)
    return head + "</head>\n<body>\n" + "\n".join(sheets) + "\n</body>\n</html>\n"


def capture_pdf(pages: list[Page], *, title: str, date_str: str,
                out_dir: Path, prefix: str = "paper", notice: str = "") -> Path:
    """全ページを1つのPDFにする。ここが副作用を持つ。"""
    from playwright.sync_api import sync_playwright

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{prefix}.html"
    html_path.write_text(build_pdf_html(pages, title=title, date_str=date_str,
                                        notice=notice), encoding="utf-8")
    pdf_path = out_dir / f"{prefix}.pdf"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-gpu"],
            env={**os.environ,
                 "CHROME_LOG_FILE": str(out_dir / f"{prefix}-chromium.log")})
        try:
            page_ctx = browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT})
            page_ctx.goto(html_path.as_uri(), wait_until="load")
            page_ctx.wait_for_timeout(200)
            page_ctx.pdf(path=str(pdf_path), width=f"{WIDTH}px",
                         height=f"{HEIGHT}px", print_background=True,
                         prefer_css_page_size=True)
        finally:
            browser.close()
    return pdf_path


# --------------------------------------------------------------------------
# 1ページ1記事の雑誌レイアウト（2026-09-16）
# --------------------------------------------------------------------------

#: カテゴリごとの差し色。白地に映える濃色でそろえる。
ACCENTS = {
    "ai": "#1f4e79", "lang": "#2e6b3f", "devtools": "#6b3fa0",
    "infra": "#0b6e7a", "industry": "#8a4b1f", "security": "#a32626",
    "gadget": "#b3651a", "keyboard": "#4a5a6a", "career": "#7a5a00",
}
DEFAULT_ACCENT = "#1f4e79"

#: 文字サイズの自動調整の範囲（px）。本文が少なければ大きく、多ければ小さく
FIT_MIN_PX = 14
FIT_MAX_PX = 28

#: この字数を超える本文は2段組にする（1行が長くなりすぎないため）
TWO_COLUMN_CHARS = 700


def build_article_sheet(article: PaperArticle, *, date_str: str,
                        labels: dict[str, str], index: int, total: int,
                        notice: str = "") -> str:
    """記事1本を1枚の紙面に組む。純粋関数。

    本文は部品（リード・前回まで・要点・本文・自分との関係・用語）に分けて
    置く。部品が欠けていれば、その枠を出さない。
    """
    sec = article_sections.parse(article.body_md)
    accent = ACCENTS.get(article.category, DEFAULT_ACCENT)
    label = html_lib.escape(labels.get(article.category, article.category or "ニュース"))
    first = article.sources[0] if article.sources else ""

    parts: list[str] = []
    parts.append(f'<div class="sheet" style="--accent:{accent}">')
    parts.append('<div class="band">'
                 f'<span class="category">{label}</span>'
                 f'<span class="date">{html_lib.escape(date_str)}</span>'
                 f'<span class="pageno">{index} / {total}</span>'
                 '<span class="paper">朝刊</span></div>')
    if notice:
        parts.append(f'<div class="notice">{html_lib.escape(notice)}</div>')
    if sec.lead:
        parts.append(f'<div class="lead">{html_lib.escape(sec.lead)}</div>')
    parts.append(f'<h1 class="headline">{_link(first, article.title)}</h1>')
    if article.image_url and safety.is_fetchable(article.image_url):
        # 元記事の代表画像。読み込めなければ枠ごと消す（onerror）
        src = html_lib.escape(article.image_url, quote=True)
        parts.append('<figure class="hero">'
                     f'<img src="{src}" alt="" onerror="this.closest(\'figure\').remove()">'
                     '<figcaption>画像: 元記事より</figcaption></figure>')
    if len(article.published_dates) > 1:
        parts.append('<div class="timeline">経過: '
                     + " → ".join(html_lib.escape(d) for d in article.published_dates)
                     + "</div>")
    if sec.recap:
        parts.append('<div class="recap"><div class="label">前回まで</div>'
                     + _md_to_html(sec.recap) + "</div>")
    if sec.points:
        items = "".join(f"<li>{html_lib.escape(pt)}</li>" for pt in sec.points)
        parts.append(f'<div class="points"><div class="label">要点</div><ul>{items}</ul></div>')
    body_class = "body two-columns" if len(sec.body) > TWO_COLUMN_CHARS else "body"
    parts.append(f'<div class="{body_class}">{_md_to_html(sec.body)}</div>')
    if sec.why:
        # 段落を持つ長めの文章になった（2026-10-01）。本文と同じ変換を通す
        parts.append('<div class="why"><div class="label">自分との関係</div>'
                     + _md_to_html(sec.why) + "</div>")
    if sec.terms:
        items = "".join(
            f"<div><dt>{html_lib.escape(t)}</dt><dd>{html_lib.escape(d)}</dd></div>"
            for t, d in sec.terms)
        parts.append(f'<div class="terms"><span class="label">用語</span><dl>{items}</dl></div>')
    if article.sources:
        links = " ／ ".join(_link(u, u) for u in article.sources)
        parts.append(f'<div class="sources"><span class="label">出典</span>{links}</div>')
    parts.append("</div>")
    return "\n".join(parts)


def build_gallery_sheet(article: PaperArticle, *, date_str: str,
                        labels: dict[str, str], index: int, total: int) -> str:
    """写真集を1枚の紙面に組む。純粋関数。

    見た目を眺めるための紙面。各項目は 写真・題名（押すと投稿）・一言。
    本文は無い。
    """
    accent = ACCENTS.get(article.category, DEFAULT_ACCENT)
    label = html_lib.escape(labels.get(article.category, article.category or "ニュース"))
    parts = [f'<div class="sheet gallery-sheet" style="--accent:{accent}">']
    parts.append('<div class="band">'
                 f'<span class="category">{label}</span>'
                 f'<span class="date">{html_lib.escape(date_str)}</span>'
                 f'<span class="pageno">{index} / {total}</span>'
                 '<span class="paper">朝刊</span></div>')
    # Slack 用の絵文字（🖼）は PDF では潰れるので落とす
    title = re.sub(r"^[^\w　-鿿]+", "", article.title).strip() or article.title
    parts.append(f'<h1 class="headline gallery-title">{html_lib.escape(title)}</h1>')
    parts.append('<div class="gallery">')
    for item in article.gallery:
        url = str(item.get("url") or "")
        image = str(item.get("image_url") or "")
        title = str(item.get("title") or "")
        note = str(item.get("note") or "")
        img = ""
        if image and safety.is_fetchable(image):
            img = (f'<img src="{html_lib.escape(image, quote=True)}" alt="" '
                   'onerror="this.classList.add(\'missing\')">')
        parts.append('<figure class="cell">'
                     f'<div class="photo">{img}</div>'
                     f'<figcaption><div class="name">{_link(url, title)}</div>'
                     + (f'<div class="note">{html_lib.escape(note)}</div>' if note else "")
                     + '</figcaption></figure>')
    parts.append('</div>')
    parts.append('<div class="sources"><span class="label">出典</span>'
                 + html_lib.escape("投稿者の写真。各項目の題名から元の投稿へ") + '</div>')
    parts.append('</div>')
    return "\n".join(parts)


def build_article_document(pages: list[Page], *, date_str: str,
                           labels: dict[str, str], notice: str = "",
                           css: str | None = None,
                           template: str | None = None) -> str:
    """全記事を1つのHTMLに。純粋関数（テストでは css / template を直接渡す）。"""
    css = css if css is not None else _load("article.css")
    template = template if template is not None else _load("article.html")
    articles = [a for page in pages for a in page.articles]
    sheets = [
        build_gallery_sheet(a, date_str=date_str, labels=labels, index=i,
                            total=len(articles))
        if a.kind == "gallery" else
        build_article_sheet(a, date_str=date_str, labels=labels, index=i,
                            total=len(articles), notice=notice if i == 1 else "")
        for i, a in enumerate(articles, start=1)
    ]
    return (template
            .replace("{{CSS}}", css)
            .replace("{{TITLE}}", html_lib.escape(f"朝刊 {date_str}"))
            .replace("{{SHEETS}}", "\n".join(sheets)))


#: ページごとに `.sheet` の font-size を二分探索で決め、収まる最大にする。
FIT_SCRIPT = """
(() => {
  const min = %d, max = %d;
  for (const sheet of document.querySelectorAll('.sheet')) {
    const fits = (px) => {
      sheet.style.fontSize = px + 'px';
      return sheet.scrollHeight <= sheet.clientHeight + 1;
    };
    let lo = min, hi = max, best = min;
    if (fits(max)) { best = max; }
    else {
      for (let i = 0; i < 12; i++) {
        const mid = (lo + hi) / 2;
        if (fits(mid)) { best = mid; lo = mid; } else { hi = mid; }
      }
    }
    sheet.style.fontSize = best.toFixed(2) + 'px';
  }
  return [...document.querySelectorAll('.sheet')].map(s => s.style.fontSize);
})()
"""


def capture_article_pdf(pages: list[Page], *, date_str: str,
                        labels: dict[str, str], out_dir: Path,
                        prefix: str = "paper", notice: str = "") -> Path:
    """雑誌レイアウトの紙面を1つのPDFにする。ここが副作用を持つ。"""
    from playwright.sync_api import sync_playwright

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{prefix}.html"
    html_path.write_text(build_article_document(pages, date_str=date_str,
                                                labels=labels, notice=notice),
                         encoding="utf-8")
    pdf_path = out_dir / f"{prefix}.pdf"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-gpu"],
            env={**os.environ,
                 "CHROME_LOG_FILE": str(out_dir / f"{prefix}-chromium.log")})
        try:
            page_ctx = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            # 外部の画像を読み終えてから文字サイズを決める（高さが変わるため）。
            # 読めない画像は onerror で枠ごと消える。長くても20秒で打ち切る
            page_ctx.goto(html_path.as_uri(), wait_until="load")
            try:
                page_ctx.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001 - 画像が遅くても紙面は作る
                pass
            page_ctx.wait_for_timeout(200)
            page_ctx.evaluate(FIT_SCRIPT % (FIT_MIN_PX, FIT_MAX_PX))
            page_ctx.wait_for_timeout(100)
            page_ctx.pdf(path=str(pdf_path), width=f"{WIDTH}px",
                         height=f"{HEIGHT}px", print_background=True,
                         prefer_css_page_size=True)
        finally:
            browser.close()
    return pdf_path


def capture(pages: list[Page], *, title: str, date_str: str,
            out_dir: Path, prefix: str = "paper", notice: str = "") -> list[Path]:
    """HTMLを書き出し、Chromiumで1ページずつPNGに撮る（目視確認用）。"""
    from playwright.sync_api import sync_playwright

    out_dir = out_dir.resolve()  # file:// にするため絶対パスへ寄せる
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with sync_playwright() as p:
        # Windows の Chromium がカレントディレクトリへ debug.log を作ることが
        # あるため、ログも生成物ディレクトリへ閉じ込める。
        browser = p.chromium.launch(
            args=["--disable-gpu"],
            env={
                **os.environ,
                "CHROME_LOG_FILE": str(out_dir / f"{prefix}-chromium.log"),
            })
        try:
            page_ctx = browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=DEVICE_SCALE)
            for page in pages:
                html_text = build_html(title=title, date_str=date_str, page=page,
                                       total_pages=len(pages), notice=notice)
                html_path = out_dir / f"{prefix}-{page.index}.html"
                html_path.write_text(html_text, encoding="utf-8")

                png_path = out_dir / f"{prefix}-{page.index}.png"
                page_ctx.goto(html_path.as_uri(), wait_until="load")
                page_ctx.wait_for_timeout(200)
                page_ctx.screenshot(path=str(png_path))
                written.append(png_path)
        finally:
            browser.close()
    return written
