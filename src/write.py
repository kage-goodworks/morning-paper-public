"""執筆（Claude / Gemini の呼び出しと降格）。

**降格そのものはモックしない。**`write.model` で失敗したら
`write.fallback_model` へ落とす判断はここが行い、モック実行でも同じ経路を通る
（`--fail claude` で実際に踏める）。

**両方が落ちたら記事化を行わない。**短い記事でごまかさず、作れなかったことを
そのまま伝える。話題は消化されないので、期限内ならもう一度押せる。

LLMの出力は検証してから使う。**検証せずにファイルへ書かない。**
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clients.parsers import llm_out
from .models import Article, Story
from .pipeline import Run

#: 執筆に失敗した原因の分類。通知の文面を変えるために使う。
KIND_CREDIT = "credit"
KIND_OTHER = "other"


class FormatError(RuntimeError):
    """LLM の出力が求めた形式でなかった。リトライの対象。"""


@dataclass
class WriteTarget:
    story: Story
    articles: list[Article] = field(default_factory=list)
    #: 前回の執筆で既に扱った記事のid。続報のとき、同じことを書かせないため。
    already: set[str] = field(default_factory=set)

    @property
    def sources(self) -> list[str]:
        return [a.url for a in self.articles]

    @property
    def image_url(self) -> str:
        """紙面に載せる代表画像。最新の記事から順に、持っているものを使う。"""
        for article in sorted(self.articles, key=lambda a: a.published_at, reverse=True):
            if article.image_url:
                return article.image_url
        return ""

    @property
    def published_dates(self) -> list[str]:
        return [a.published_at[:10] for a in self.articles if a.published_at]

    @property
    def is_followup(self) -> bool:
        return bool(self.already)


@dataclass
class WriteResult:
    story_id: str
    title: str
    body_md: str
    sources: list[str]
    published_dates: list[str]
    model: str
    degraded: bool = False
    followup: bool = False
    category: str = ""
    image_url: str = ""


@dataclass
class WriteOutcome:
    """執筆の結果。失敗したときは `results` が空になる。"""
    ok: bool
    results: list[WriteResult] = field(default_factory=list)
    kind: str = ""
    reason: str = ""
    degraded: bool = False
    model: str = ""


def build_prompt(targets: list[WriteTarget], *, char_range: list[int],
                 hint_count: int, profile: str = "",
                 topics: list[str] | None = None) -> str:
    """執筆プロンプトを組み立てる。純粋関数。

    `profile` は読み手（本人）の像。「自分との関係」をありきたりな一般論で
    なく、本人の開発と将来に引き寄せて書かせるために渡す（本人の要望、
    2026-10-01）。`config.yml` の `write.reader_profile`。

    続報の話題では、前回扱った記事も**「扱い済み」と印を付けて**渡す。
    前回書いた文章そのものは保存していないが、どの記事まで書いたかは分かるので、
    新しい記事の内容だけを書かせられる。文字列の差分は取らない ―― 別のメディアが
    書いた別記事なので、機械的に比べても新しい情報は取り出せないため。

    個人の投稿本文とアカウント名は渡さない（AC-18）。
    """
    low, high = (list(char_range) + [600, 900])[:2]
    lines = [
        "次の話題それぞれについて、日本語の解説記事を書いてください。",
        "読み手は IT に詳しくない個人です。専門用語は避けるか、TERMS で一言説明します。",
        "文体は新聞・雑誌の「である調」。見出し風の体言止めは POINTS と LEAD だけに使います。",
        "",
        "1本は次の部品で構成し、部品名を行頭に書いてから中身を書きます。",
        "LEAD: 何が起きたかを一言で（40字以内、体言止め）",
        "RECAP: 続報の話題（STATUS 行に続報とあるもの）のときだけ。前回までの経緯をまとめる"
        "（見出しや「前回まで:」の文字は書かず、中身だけ）。読み手は前回の記事を覚えていない前提で、必要なら数段落使ってよい。"
        "続報でなければこの部品は書かない",
        "POINTS: 要点を3つ、箇条書き（各1行、体言止め）",
        f"BODY: 本文（{low}〜{high}字）。事実 → なぜ重要か → 世間の反応の順。"
        "段落は空行で区切る。数字や日程、新旧の比較が並ぶときは Markdown の表を使う"
        "（表は最大1つ）。反応は出典が0件なら「目立った反応は見つからなかった」",
        "WHY: 「自分との関係」。PROFILE の読み手に向けて、次の3点を具体的に書く"
        "（250〜400字。段落は空行で区切ってよい）。",
        "  (1) 読み手の開発・仕組みにどう関係し、どこにどう取り込める・応用できるか。"
        "具体的な使い方を1つ以上",
        "  (2) 読み手が将来どの分野・役割で活かせるか",
        "  (3) 既存のシステムや慣行に与える影響と、市場・社会への波及の推測"
        "（推測と分かる書き方で）",
        "  「動向を注視したい」「影響がありそう」のような、言われなくても分かる一般論は"
        "書かない。読んで「そういう使い方があるのか」という発見が1つは残るように。"
        "PROFILE に無いことは決めつけず、条件付きで書く",
        "TERMS: 本文に出た専門用語を2〜3語、「用語: 一言説明」の箇条書き。無ければ省略",
        "TOPIC: この話題が属する**知識の主題**の名前を1つ（例: Claude Code、GitHub Actions、"
        "ポスト量子暗号、OpenAI）。記事の見出しではなく、製品名・技術名・会社名・分野名の粒度。"
        "下の「既存の主題」に当てはまるものがあれば、その名前を一字一句そのまま使う。"
        "無ければ新しい名前を付ける（30字以内）",
        "KNOW: 後から知識として参照できる事実を2〜4個、箇条書き（各1行）。"
        "記事の言い回しではなく「何が・いつ・どう変わったか」を、日付・数値・固有名詞を残して書く。"
        "推測は書かない",
        "",
        "反応を創作しないでください。渡した情報にない反応は書かないこと。",
        "個人のアカウント名と投稿本文は書かないこと。",
        "[扱い済み]と印の付いた記事は前回すでに記事にしています。"
        "その内容は RECAP にまとめ、BODY には[新規]の記事で分かった新しい情報だけを書いてください。",
        "各記事の先頭に <!-- ARTICLE n --> を付けて区切ってください。",
        "",
    ]
    if topics:
        lines.append("--- 既存の主題（TOPIC はなるべくこの中から選ぶ） ---")
        lines.append("、".join(topics))
        lines.append("")
    if profile.strip():
        lines.append("--- PROFILE（読み手） ---")
        lines.extend(line.rstrip() for line in profile.strip().splitlines())
        lines.append("")
    for index, target in enumerate(targets, 1):
        lines.append(f"--- 話題 {index} ---")
        lines.append("TITLE: " + target.story.title)
        lines.append("CATEGORY: " + target.story.category)
        if target.is_followup:
            lines.append("STATUS: 続報（前回の記事の続き）")
        for article in target.articles:
            mark = "[扱い済み]" if article.id in target.already else "[新規]"
            lines.append(f"  {mark} [{article.published_at}] {article.title}")
            lines.append(f"    URL: {article.url}")
            reactions = reaction_line(article)
            if reactions:
                lines.append("    REACTIONS: " + reactions)
            body = (article.body_md or "").strip()
            lines.append("    BODY: " + (body[:1200] if body else "(本文なし)"))
        lines.append("")
    lines.append(f"COUNT: {len(targets)}")
    return "\n".join(lines)


def build_gallery_prompt(target: WriteTarget) -> str:
    """写真集の各項目に付ける一言の指示文。純粋関数。

    見た目を眺めるための紙面なので、解説は書かせない。投稿の題名と本文から
    「ここがこだわり」を40字以内で1行。投稿者名は渡さない（AC-18）。
    """
    lines = [
        "次の投稿それぞれについて、写真に添える一言を日本語で書いてください。",
        "内容は「ここがこだわり」「何が珍しいか」を40字以内、体言止めか短い1文。",
        "製品名や配列、キーキャップ、ケースの素材など、見た目に関わる特徴を優先します。",
        "分からないことは推測せず、題名から言えることだけを書きます。",
        "出力は「番号. 一言」を1行に1件、入力と同じ番号で、同じ件数だけ。前置きや空行は不要。",
        "",
    ]
    for index, article in enumerate(target.articles, start=1):
        lines.append(f"{index}. TITLE: {article.title}")
        text = " ".join((article.body_md or "").split())[:300]
        if text:
            lines.append(f"   TEXT: {text}")
    return "\n".join(lines)


def write_gallery_notes(run: Run, target: WriteTarget) -> dict[int, str]:
    """写真集の一言を書く。失敗したら空（一言なしで写真だけ並べる）。"""
    from .extract import parse_translations   # 「番号. 文」の突き合わせを共用

    config = run.config
    prompt = build_gallery_prompt(target)
    for model_name in (str(config.get("write.model", "claude")),
                       str(config.get("write.fallback_model", "gemini"))):
        client = run.clients.llms.get(model_name)
        if client is None:
            continue
        raw = run.guarded(
            lambda client=client: client.complete(prompt, "gallery"),
            target=f"{model_name}（写真集の一言）",
            impact="写真集の一言が付きません",
            default=None, status="")
        if raw:
            notes = parse_translations(raw, len(target.articles))
            if notes:
                return notes
    run.note_status("写真集: 一言を書けなかったため写真と題名だけにしています")
    return {}


def reaction_line(article: Article) -> str:
    """記事の反応を1行に。純粋関数。

    「世間の反応」を書かせるのに必要な材料。数だけを渡し、個人の投稿本文と
    アカウント名は渡さない（AC-18）。X の要約は傾向の文章なので渡す。
    反応が何も無ければ空（プロンプトに行を足さない）。
    """
    r = article.reactions
    parts: list[str] = []
    if r.hn_points or r.hn_comments:
        parts.append(f"Hacker News {r.hn_points}点・コメント{r.hn_comments}件")
    if r.hatena:
        parts.append(f"はてなブックマーク {r.hatena}件")
    if r.reddit:
        parts.append(f"Reddit {r.reddit}点")
    if r.x_summary:
        parts.append("X上の傾向: " + " ".join(r.x_summary.split())[:300])
    return " / ".join(parts)


def _classify(run: Run, model_name: str) -> str:
    """直近の失敗がクレジット切れかどうかを見る。

    分類そのものは `retry.classify` が行う。実際のAPIが返す文言に合わせた
    確認は、実キーを入れる段階で行う。
    """
    for failure in reversed(run.failures):
        if model_name in failure.target:
            return KIND_CREDIT if failure.kind == "credit" else KIND_OTHER
    return KIND_OTHER


def reader_profile(config) -> str:
    """本人の像。`write.reader_profile` か、`write.reader_profile_file` のファイル。

    公開リポジトリでは本文を config に書かず、非公開データのファイルから読む
    （2026-10-31）。どちらも無ければ空（一般論で書く）。
    """
    text = str(config.get("write.reader_profile", "") or "")
    if text:
        return text
    rel = str(config.get("write.reader_profile_file", "") or "")
    path = config.root / rel if rel else None
    if path is not None and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def write_articles(run: Run, targets: list[WriteTarget], *,
                   topics: list[str] | None = None) -> WriteOutcome:
    """記事を書く。失敗したら降格し、それも駄目なら**書かずに終わる**。"""
    if not targets:
        return WriteOutcome(ok=True)

    config = run.config
    primary = str(config.get("write.model", "claude"))
    fallback = str(config.get("write.fallback_model", "gemini"))
    char_range = list(config.get("limits.article_chars", [600, 900]))
    hint_count = int(config.get("limits.career_hint_per_request", 1))
    profile = reader_profile(config)

    prompt = build_prompt(targets, char_range=char_range, hint_count=hint_count,
                          profile=profile, topics=topics)

    for model_name, degraded in ((primary, False), (fallback, True)):
        client = run.clients.llms.get(model_name)
        if client is None:
            continue

        def attempt(client=client) -> llm_out.ArticleSplit:
            # 形式の不正（記事が4本しか返らない等）も失敗として投げ、通信の
            # 失敗と同じくリトライに乗せる。1回の形式崩れで降格まで落ちると、
            # 降格先が混雑している朝はまとまりごと作れない（2026-09-30 に発生）
            raw = client.complete(prompt, "write")
            split = llm_out.split_articles(raw, expected=len(targets))
            if not split.ok:
                raise FormatError(f"出力形式が不正（{split.reason}）")
            return split

        split = run.guarded(
            attempt,
            target=f"{model_name}（記事の執筆）",
            impact=(f"{fallback}へ降格して執筆します" if not degraded
                    else "記事を作成せず、作れなかったことを通知します"),
            default=None, status=None)

        if split is None:
            if not degraded:
                run.note_status(f"執筆: {primary}が応答しないため{fallback}へ降格しました")
            continue

        if degraded:
            run.note_status(f"執筆: {fallback}で書きました（{primary}から降格）")

        return WriteOutcome(
            ok=True, degraded=degraded, model=model_name,
            results=[
                WriteResult(story_id=t.story.id, title=t.story.title,
                            body_md=body, sources=t.sources,
                            published_dates=t.published_dates,
                            model=model_name, degraded=degraded,
                            followup=t.is_followup,
                            category=t.story.category,
                            image_url=t.image_url)
                for t, body in zip(targets, split.bodies)
            ])

    kind = KIND_CREDIT if (_classify(run, primary) == KIND_CREDIT
                           or _classify(run, fallback) == KIND_CREDIT) \
        else KIND_OTHER
    run.note_status("執筆: 両方のモデルが使えないため記事を作成しませんでした")
    return WriteOutcome(ok=False, kind=kind,
                        reason=f"{primary}と{fallback}の両方が応答しませんでした")


def failure_notice(outcome: WriteOutcome, *, count: int) -> str:
    """記事を作れなかったことを伝える文面。原因で書き分ける。"""
    if outcome.kind == KIND_CREDIT:
        head = ":warning: クレジットがなくなったことにより記事が作れませんでした"
        detail = ("AIの利用枠が尽きています。補充すると再び記事を作れます。")
    else:
        head = ":warning: 記事が作れませんでした"
        detail = ("AIが応答しませんでした。時間をおいてもう一度お試しください。")
    return "\n".join([
        head, "",
        detail,
        f"対象の{count}件はチェックが付いたまま残しています。"
        "期限内であれば、あとからもう一度ボタンを押せます。",
        "",
        "元記事のURLは続けて投稿します。",
    ])


def degraded_notice(outcome: WriteOutcome, *, primary: str) -> str:
    """降格したことを1文で伝える。紙面とスレッドの両方に出す。

    記事は1回の呼び出しでまとめて書くため、降格は**その回の全記事に一律**で
    効く。「途中から切り替わる」ことはない。
    """
    if not outcome.degraded:
        return ""
    return (f"この回の記事は{outcome.model}が書いています"
            f"（{primary}が応答しなかったため）")
