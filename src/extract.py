"""本文のMarkdown化（副作用あり）。

RSS全文 → trafilatura → Jina Reader → Firecrawl の順に試し、最初に成功した
ものを採用する（FR-1）。**このフォールバックはモックしない。**モック実行でも
同じここを通り、フィクスチャの歯抜けによって各段が実際に踏まれる。

整形はコード処理で行い、LLMは使わない。YouTubeだけはGeminiの要約を本文とする。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import safety
from .models import Article, ExtractResult
from .pipeline import Run


def _is_youtube(article: Article) -> bool:
    return "youtube.com" in article.url or "youtu.be" in article.url


#: 本文取得全体に使ってよい秒数の既定。`limits.extract_seconds` で上書き。
#: 朝のジョブは15分以内（NFR-6）。収集・翻訳・反応・投稿の分を残して6分。
DEFAULT_EXTRACT_SECONDS = 360

#: 期限を過ぎて取得を見送った記事に付ける印
SKIPPED_DEADLINE = "extract_skipped_deadline"


def extract_one(run: Run, article: Article,
                deadline: float | None = None) -> Article:
    """1本の本文を埋める。4段とも失敗したら見出しと出典URLだけで残す。

    `deadline`（`time.monotonic()` の値）を過ぎていたら、残りの段へは進まず
    見出しと出典URLだけで返す。遅いサイトが何件あっても朝の処理を15分に
    収めるため。
    """
    if deadline is not None and time.monotonic() >= deadline:
        article.extracted_by = "none"
        article.errors.append(SKIPPED_DEADLINE)
        return article

    if _is_youtube(article):
        # 動画ごとの失敗は Slack へ通知しない。Gemini は非公開・メンバー限定・
        # 年齢制限・配信前の動画を「403 PERMISSION_DENIED」で断る。鍵の問題では
        # ないのに「キーの期限切れかも」と毎回通知が出て紛らわしかった
        # （2026-09-25・10-02）。件数だけを取得状況に載せる（extract_all）
        summary = run.guarded(
            lambda: run.clients.llms["gemini"].complete(
                f"URL: {article.url}\nTITLE: {article.title}", "youtube"),
            target="Gemini（YouTube動画の要約）",
            impact="その動画の要約が載りません",
            default="", status="", notify=False)
        if summary:
            article.body_md = summary
            article.extracted_by = "gemini_youtube"
            return article
        article.errors.append("youtube_summary_failed")
        return article

    for stage in run.clients.extractors:
        if deadline is not None and time.monotonic() >= deadline:
            article.extracted_by = "none"
            article.errors.append(SKIPPED_DEADLINE)
            return article
        result: ExtractResult | None = run.guarded(
            lambda stage=stage: stage.extract(article),
            target=f"{stage.name}（本文取得）",
            impact="次の手段へ進みます",
            default=None,
            status="",      # 記事ごとの行は出さない。集計は extract_all が書く
            notify=False)   # 次の手段へ自動で進むので人に知らせない
        if result is not None and result.ok and result.body_md.strip():
            article.body_md = result.body_md
            article.extracted_by = result.extracted_by
            article.outbound_links = result.outbound_links
            if result.image_url and safety.is_fetchable(result.image_url):
                article.image_url = result.image_url
            return article

    article.extracted_by = "none"
    article.errors.append("extract_failed")
    return article


def extract_all(run: Run, articles: list[Article]) -> list[Article]:
    """並列で本文を埋める。朝のジョブを15分以内に収めるため（NFR-6）。

    全体に時間の上限を置く。期限を過ぎた記事は見出しと出典URLだけで
    一覧に載せ、その旨を `取得状況` に書く。**本文が無くても一覧は出す**
    （縮退の原則）。
    """
    budget = float(run.config.get("limits.extract_seconds",
                                  DEFAULT_EXTRACT_SECONDS))
    deadline = time.monotonic() + budget
    with ThreadPoolExecutor(max_workers=8) as pool:
        out = list(pool.map(lambda a: extract_one(run, a, deadline), articles))

    skipped = [a for a in out if SKIPPED_DEADLINE in a.errors]
    if skipped:
        run.note_status(
            f"本文取得: 時間の上限（{budget:.0f}秒）に達したため"
            f"{len(skipped)}件を見送りました（見出しと出典URLのみ）")

    failed = [a for a in out if a.extracted_by == "none"
              and SKIPPED_DEADLINE not in a.errors]
    if failed:
        run.note_status(
            f"本文取得: {len(failed)}件が4段とも失敗（見出しと出典URLのみ）")

    unsummarized = [a for a in out if "youtube_summary_failed" in a.errors]
    if unsummarized:
        run.note_status(
            f"YouTube: {len(unsummarized)}件の動画は要約できませんでした"
            "（非公開・限定・配信前などで読めない動画。題名とURLだけ載せています）")

    by_stage: dict[str, int] = {}
    for article in out:
        by_stage[article.extracted_by] = by_stage.get(article.extracted_by, 0) + 1
    detail = " / ".join(f"{k} {v}件" for k, v in sorted(by_stage.items()))
    run.note_status(f"本文取得の内訳: {detail}")
    return out


#: 1回の呼び出しで扱う見出しの数。全記事（150〜250件）を1回で渡すと出力が
#: 長くなりすぎて途中で切れるため、分けて呼ぶ。呼び出し回数は増えるが
#: LLM を使う箇所（翻訳・執筆・動画要約）は増えていない
HEADLINE_BATCH = 60


def translate_titles(run: Run, articles: list[Article]) -> list[Article]:
    """見出しを日本語にし、短い見出し（`title_short`）を付ける（FR-3 / AC-25）。

    1回の呼び出しで**訳文と短縮見出しの両方**を返させる。

    - 英語の見出しは訳文で `title` を置き換える（原題は `title_original`）。
      日本語の見出しは `title` を触らない
    - 短縮見出しは全記事に付け、一覧と紙面の題名に使う（本人の要望、
      2026-10-01: 重要な単語だけを残した必要最低限の形）。Vault のノートは
      `title` のままなので、記事そのものは変わらない

    失敗した分は原題のまま・短縮なしで出す。
    """
    targets = [a for a in articles if (a.title or "").strip()]
    if not targets:
        return articles

    english = sum(1 for a in targets if _looks_english(a.title))
    translated = 0
    shortened = 0
    for start in range(0, len(targets), HEADLINE_BATCH):
        chunk = targets[start:start + HEADLINE_BATCH]
        prompt = build_translate_prompt([a.title for a in chunk])
        result = run.guarded(
            lambda prompt=prompt: run.clients.llms["gemini"].complete(prompt, "translate"),
            target="Gemini（見出しの翻訳と短縮）",
            impact="英語のタイトルが原題のまま、見出しも短くならずに出ます",
            default="")
        parsed = parse_headlines(result or "", len(chunk))
        for index, article in enumerate(chunk, start=1):
            pair = parsed.get(index)
            if pair is None:
                continue
            full, short = pair
            if full and _looks_english(article.title) and full != article.title:
                article.title_original = article.title
                article.title = full
                translated += 1
            if short:
                article.title_short = short
                shortened += 1

    if not translated and not shortened:
        run.note_status("見出し: 翻訳と短縮に失敗したため原題のまま出しています")
        return articles
    line = f"見出し: {shortened}件を短くしました"
    if english:
        line += f"（英語{english}件のうち{translated}件を日本語に）"
    run.note_status(line)
    return articles


#: 訳文と短縮見出しの区切り
HEADLINE_SEP = "||"


def build_translate_prompt(titles: list[str]) -> str:
    """翻訳と短縮の指示文。純粋関数。

    番号付きで渡し、番号付きで返させる。LLM は前置きや空行を足すことが
    あるので、行数ではなく番号で突き合わせる。`TITLE:` の行はモックが
    数えるために残す。**本番の初回は指示が無く、Gemini が何をすべきか
    分からないまま返して行数が合わず失敗していた。**

    1行に「訳文 || 短縮見出し」の2つを書かせる。翻訳と短縮を別の呼び出しに
    分けない（LLM を使う箇所を増やさない）。
    """
    numbered = "\n".join(f"{i}. TITLE: {t}" for i, t in enumerate(titles, start=1))
    return (
        "次のニュース見出しそれぞれについて、2つを書いてください。\n"
        "A. 訳文: 英語の見出しは自然な日本語の見出しに翻訳する。固有名詞・製品名・"
        "バージョン番号はそのまま残し、意訳せず、情報を足したり削ったりしない。"
        "日本語の見出しはそのまま写す。\n"
        "B. 短縮見出し: 重要な単語（固有名詞・製品名・数字・何が起きたかを示す語）だけを"
        "切り抜いてつなげた必要最低限の形。目安は10〜20字。助詞・修飾語・煽り文句・"
        "媒体名・「〜について」「〜を発表」のような定型は省き、「大まかに言うとこれ」が"
        "一目で分かるようにする。意味は変えない。英語の見出しも日本語で書く。\n"
        f"出力は「番号. 訳文 {HEADLINE_SEP} 短縮見出し」を1行に1件、入力と同じ番号で、"
        "同じ件数だけ。前置き・説明・空行・TITLE: の文字は付けないでください。\n\n"
        + numbered
    )


_NUMBERED = re.compile(r"^\s*(\d+)\s*[.．)）:：]\s*(.+?)\s*$")


def parse_headlines(text: str, count: int) -> dict[int, tuple[str, str]]:
    """`番号. 訳文 || 短縮見出し` の行を番号で拾う。純粋関数。

    区切りが無い行は「訳文＝短縮見出し」として扱う（区切りを忘れた出力や、
    モックの `[翻訳]…` に耐える）。番号が1つも無く行数が一致するときだけ
    行順で対応づける（`parse_translations` と同じ規則）。
    """
    def split(value: str) -> tuple[str, str]:
        value = value.removeprefix("TITLE:").strip()
        if HEADLINE_SEP in value:
            full, _, short = value.partition(HEADLINE_SEP)
            full, short = full.strip(), short.strip()
            return (full or short, short or full)
        return (value, value)

    lines = [line for line in (text or "").splitlines() if line.strip()]
    out: dict[int, tuple[str, str]] = {}
    for line in lines:
        m = _NUMBERED.match(line)
        if not m:
            continue
        number = int(m.group(1))
        pair = split(m.group(2))
        if 1 <= number <= count and pair[0]:
            out[number] = pair
    if out:
        return out
    if len(lines) == count:
        return {i: split(line) for i, line in enumerate(lines, start=1)}
    return {}


def parse_translations(text: str, count: int) -> dict[int, str]:
    """`番号. 訳文` の行を番号で拾う。純粋関数。

    番号が無い行（前置きなど）は捨てる。範囲外の番号も捨てる。モックは
    番号なしで `[翻訳]…` を件数ぶん返すので、番号が1つも無く行数が
    一致するときだけ行順で対応づける。
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    out: dict[int, str] = {}
    for line in lines:
        m = _NUMBERED.match(line)
        if not m:
            continue
        number = int(m.group(1))
        value = m.group(2).removeprefix("TITLE:").strip()
        if 1 <= number <= count and value:
            out[number] = value
    if out:
        return out
    if len(lines) == count:
        return {i: line.strip() for i, line in enumerate(lines, start=1)}
    return {}


def _looks_english(title: str) -> bool:
    """日本語文字を含まないものを英語とみなす。"""
    if not title:
        return False
    for ch in title:
        if "぀" <= ch <= "ヿ" or "一" <= ch <= "鿿":
            return False
    return any(ch.isascii() and ch.isalpha() for ch in title)
