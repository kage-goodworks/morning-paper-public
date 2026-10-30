"""`tests/fixtures/` を生成する開発用スクリプト。

外部へ接続できないため、フィクスチャは**実データと同じ形の合成データ**で作る。
実キー投入の段階で、本物の取得結果へ差し替えること（README に明記）。

生成するもの:

- `rss/dayN/<source>.xml`   プロバイダー別の生XML（RSS 2.0 / Atom / YouTube）
- `pages/<url_hash>.html`   記事HTML。一部はわざと欠けさせて4段のフォールバックを踏ませる
- `reactions/<url_hash>.json` HN・はてブ・Reddit の生レスポンス
- `grok/*.json`             citations が空のケースを含む
- `articles/*.md`           執筆モックが返す定型記事
- `releases/*.json` `stooq.csv` `slack/block_actions.json`
- `scenarios.yml`           3日間の状態変化を表すシナリオ定義

シナリオ: 通常系 / 収集0件 / 部分障害 / 繰り越し / 既存話題への統合 / 記事化済みの除外
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))

from src.config import load as load_config  # noqa: E402


def sources_by_category() -> dict[str, list[dict]]:
    """`config.yml` の情報源をカテゴリごとに並べる。

    フィクスチャのファイル名は情報源のidと一致していないと、モックが
    読みに行っても見つからない。**情報源リストを差し替えたらここも
    自動で追随する**よう、設定から導出する。
    """
    grouped: dict[str, list[dict]] = {}
    for source in load_config().get("sources", []) or []:
        grouped.setdefault(str(source.get("category")), []).append(source)
    return grouped


CONFIG_SOURCES = sources_by_category()


def assign(category: str, index: int) -> dict:
    """そのカテゴリの情報源へ順番に割り当てる。"""
    pool = CONFIG_SOURCES.get(category) or CONFIG_SOURCES.get("industry") or []
    if not pool:
        return {"id": category, "fulltext": False}
    return pool[index % len(pool)]


def uhash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# (source_id, category, title, 日付, 本文に置く外部リンク)
# 1日目。ai を18本入れて per_category=12 を超えさせ、6本を繰り越しにする。
DAY1: list[tuple[str, str, str]] = [
    ("openai", "ai", "OpenAI が GPT-5 の一般提供を開始"),
    ("techcrunch", "ai", "OpenAI GPT-5 の価格体系に開発者から異論"),
    ("anthropic", "ai", "Anthropic が Claude の長文脈モードを拡張"),
    ("itmedia", "ai", "国内企業の生成AI導入率が42%に"),
    ("openai", "ai", "OpenAI が推論モデルの評価手法を公開"),
    ("anthropic", "ai", "Anthropic が MCP の仕様更新を発表"),
    ("techcrunch", "ai", "画像生成スタートアップが資金調達を完了"),
    ("itmedia", "ai", "AI エージェント向けの評価基盤が公開"),
    ("openai", "ai", "OpenAI が音声モデルの遅延を半減"),
    ("anthropic", "ai", "Claude の推論コストが引き下げ"),
    ("techcrunch", "ai", "オープンウェイトモデルの採用が拡大"),
    ("itmedia", "ai", "AI 規制の国内ガイドラインが改訂"),
    ("openai", "ai", "OpenAI が開発者向けダッシュボードを刷新"),
    ("anthropic", "ai", "長文脈モデルの検索精度に関する論文"),
    ("techcrunch", "ai", "推論チップの新製品が発表"),
    ("itmedia", "ai", "生成AIの社内利用ポリシー策定が進む"),
    ("openai", "ai", "OpenAI が安全性評価の結果を公表"),
    ("anthropic", "ai", "Anthropic がモデル蒸留の新手法を公開"),
    ("openai", "ai", "OpenAI が長文脈の検索精度を改善"),
    ("anthropic", "ai", "Claude の日本語性能に関する検証"),
    ("techcrunch", "ai", "LLM の推論最適化ライブラリが公開"),
    ("itmedia", "ai", "機械学習基盤の運用事例が公開"),
    ("openai", "ai", "OpenAI が埋め込みモデルを更新"),
    ("anthropic", "ai", "Claude Code の利用統計が公開"),
    ("rustblog", "lang", "Rust 1.90 リリース"),
    ("pythonblog", "lang", "Python 3.14 の変更点まとめ"),
    ("rustblog", "lang", "Cargo のワークスペース機能が改善"),
    ("pythonblog", "lang", "TypeScript 6.0 のベータが公開"),
    ("ghblog", "devtools", "GitHub Actions のキャッシュが高速化"),
    ("ghblog", "devtools", "VSCode の拡張機能APIが更新"),
    ("ghblog", "devtools", "Neovim 0.12 がリリース"),
    ("aws", "infra", "AWS が新しいコンテナランタイムを発表"),
    ("aws", "infra", "PostgreSQL 18 の性能改善"),
    ("aws", "infra", "Kubernetes 1.34 の主な変更"),
    ("publickey", "industry", "大手SIerがクラウド事業を分社化"),
    ("publickey", "industry", "国内スタートアップの資金調達額が回復"),
    ("techcrunch", "industry", "半導体大手が製造子会社を買収"),
    ("sectracker", "security", "OpenSSL に深刻な脆弱性 CVE-2026-1234"),
    ("sectracker", "security", "ランサムウェア被害が製造業で増加"),
    ("sectracker", "security", "多要素認証の回避手口が報告"),
    ("gadgetwire", "gadget", "ThinkPad の新モデルが発表"),
    ("gadgetwire", "gadget", "USB4 対応ドックの比較"),
    ("keebnews", "keyboard", "自作キーボードの新軸がレビューされる"),
    ("keebnews", "keyboard", "キーキャップの新素材が登場"),
    ("devcareer", "career", "国内開発者の年収調査2026が公開"),
    ("devcareer", "career", "未経験からの転職事例まとめ"),
]

# 2日目。1日目に繰り越された OpenAI GPT-5 の話題への続報を含む（統合を踏む）。
DAY2: list[tuple[str, str, str]] = [
    ("itmedia", "ai", "OpenAI GPT-5 の提供地域が拡大 追加の価格改定も"),
    ("publickey", "ai", "GPT-5 の性能検証結果が出そろう"),
    # 1日目に繰り越された話題への続報。固有名詞が重なり統合される（AC-7）。
    ("anthropic", "ai", "Anthropic がモデル蒸留の適用結果を公開"),
    ("itmedia", "ai", "Claude の推論コスト引き下げによる影響"),
    ("publickey", "ai", "Claude Code の利用統計に関する分析"),
    ("techcrunch", "ai", "Claude の日本語性能について追加の検証"),
    ("anthropic", "ai", "Anthropic が企業向けプランを改定"),
    ("rustblog", "lang", "Rust 1.90 の非互換変更に関する補足"),
    ("aws", "infra", "AWS の新リージョンが稼働開始"),
    ("sectracker", "security", "CVE-2026-1234 の修正版が配布開始"),
    ("publickey", "industry", "クラウド市場の四半期シェアが公開"),
    ("gadgetwire", "gadget", "新型ノートPCの実機レビュー"),
]

# 3日目。さらに続報。統合が2日にわたって効くことを確認する。
DAY3: list[tuple[str, str, str]] = [
    ("techcrunch", "ai", "OpenAI GPT-5 の企業導入事例が公開"),
    ("openai", "ai", "OpenAI が GPT-5 の制限緩和を告知"),
    ("pythonblog", "lang", "Python 3.14 の移行ガイドが公開"),
    ("ghblog", "devtools", "GitHub Copilot の新機能が追加"),
    ("aws", "infra", "コンテナランタイムの一般提供が開始"),
    ("devcareer", "career", "開発者の学習時間に関する調査"),
]

DAYS = {1: ("2026-09-01", DAY1), 2: ("2026-09-02", DAY2), 3: ("2026-09-03", DAY3)}

# 英語タイトル（翻訳経路 AC-25 を踏む）
ENGLISH = {
    "Rust 1.90 リリース": "Announcing Rust 1.90",
    "Python 3.14 の変更点まとめ": "What is new in Python 3.14",
    "Neovim 0.12 がリリース": "Neovim 0.12 released",
}

RSS_ITEM = """    <item>
      <title>{title}</title>
      <link>{link}</link>
      <pubDate>{pub}</pubDate>
{content}    </item>
"""

RSS_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{source}</title>
    <link>https://example.invalid/{source}</link>
    <description>{source} feed</description>
{items}  </channel>
</rss>
"""

ATOM_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>{source}</title>
{items}</feed>
"""

ATOM_ENTRY = """  <entry>
    <title>{title}</title>
    <link href="{link}"/>
    <published>{pub}</published>
  </entry>
"""

GALLERY_TITLES = ["Seal x OG Doubleshot", "Mode Encore in navy", "Tofu 60 2.0 RAW",
                  "my first split build", "Slate75 Copper Edition", "MTNU on a brass Bakeneko",
                  "hand-wired 40% with trackpoint", "Keychron Q5 Ultra + G5 Ultra Light"]

GALLERY_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <title>{source}</title>
{entries}</feed>
"""

GALLERY_ENTRY = """  <entry>
    <title>{title}</title>
    <link href="{link}"/>
    <published>{pub}</published>
    <media:thumbnail url="{image}"/>
    <content type="html">&lt;table&gt;&lt;tr&gt;&lt;td&gt;&lt;a href="{link}"&gt;&lt;img src="{image}"/&gt;&lt;/a&gt;&lt;/td&gt;&lt;td&gt;{text} submitted by /u/someone &lt;a href="{link}"&gt;[link]&lt;/a&gt; &lt;a href="{link}"&gt;[comments]&lt;/a&gt;&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</content>
  </entry>
"""

YT_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <title>sample_channel_a</title>
  <entry>
    <title>週刊AIニュース解説</title>
    <yt:videoId>vid0001</yt:videoId>
    <published>{pub}T09:00:00+09:00</published>
  </entry>
</feed>
"""

PAGE = """<!doctype html>
<html lang="ja"><head><title>{title}</title></head>
<body>
<nav class="global-nav"><a href="https://example.invalid/">ホーム</a> 検索 ログイン</nav>
<div class="ad-banner">広告 いますぐ登録</div>
<article>
  <h1>{title}</h1>
  <p>{lead}</p>
  <p>{body}</p>
  <p>関連情報は<a href="{outlink}">こちらの発表</a>を参照。</p>
  <aside class="related-articles">関連記事 おすすめ</aside>
</article>
<footer>会社概要 プライバシーポリシー</footer>
</body></html>
"""

# 執筆モックが返す定型記事。部品（LEAD/RECAP/POINTS/BODY/WHY/TERMS）の
# 有無と長さを変え、紙面の文字サイズ調整と部品の欠けを確かめられるようにする。
# 5本目だけは部品なし（降格先のモデルや古い出力の形）。
ARTICLE_MDS = {
    1: '''LEAD: 開発者向け提供枠が拡大、条件も変更
POINTS:
- {title}を{date}付で発表
- 機能追加と提供条件の変更を同時に告知
- 各社の条件がそろい、選定基準は運用のしやすさへ
BODY:
{title}が発表された。発表は{date}付で、対象は開発者向けの提供枠にあたる。公開された情報の範囲では、機能の追加と提供条件の変更が同時に告知されている。

この動きが重要なのは、同じ領域の各社が提供条件をそろえはじめている点にある。価格と提供地域の差が縮まると、選定の基準は性能そのものより運用のしやすさへ移る。昨年からの流れを踏まえると、今回の発表はその転換点に位置づけられる。

| 項目 | これまで | 今回 |
|---|---|---|
| 提供地域 | 3地域 | 12地域 |
| 上限 | 1日1,000回 | 1日10,000回 |
| 料金 | 従量のみ | 定額プランを追加 |

反応は評価が割れている。性能の向上を評価する声がある一方、既存の利用者からは移行の手間を懸念する指摘が出ている。
WHY: 個人で試す範囲なら、今回の定額プランで費用の見通しが立てやすくなる。すでに使っているなら、移行期限だけ確認しておけばよい。
TERMS:
- 提供枠: 誰がどの条件で使えるかの区分
- 従量課金: 使った分だけ払う方式
''',
    2: '''LEAD: 主要3社が同じ週に新モデルを公開
RECAP:
前回まで: 先月、各社は推論性能を競う新モデルを相次いで予告していた。{title}はその中で最も早く公開日を示し、開発者向けの先行提供を始めていた。

先行提供の段階では、長文の扱いと応答速度の改善が中心で、価格は据え置きだった。
POINTS:
- 一般提供が開始、先行提供の制限を撤廃
- 価格は据え置き、上限だけ引き上げ
- 競合2社も同週に追随
BODY:
{title}の一般提供が始まった。先行提供で設けられていた利用上限は撤廃され、価格は据え置かれた。

同じ週に競合2社も新モデルを公開しており、性能の数字だけでは差がつきにくくなっている。選ぶ側にとっては、周辺の道具のそろい方と、既存の仕組みへの組み込みやすさが判断材料になる。

反応は概ね好意的だが、先行提供で見つかった不具合が一般提供でも残っているという報告が複数ある。
WHY: すでに先行提供で使っていたなら、設定を変えずにそのまま使い続けられる。新規に試すなら、競合との比較は数字より自分の用途で決めるのがよい。
TERMS:
- 一般提供: 誰でも申し込めば使える状態
''',
    3: '''LEAD: 脆弱性の修正版が公開、更新を推奨
POINTS:
- {title}で深刻度「高」の脆弱性を修正
- 影響は特定の設定を使う環境に限られる
- 自動更新が有効なら操作は不要
BODY:
{title}が公開された。修正されたのは深刻度「高」の脆弱性で、悪用されると外部から任意の操作を実行される恐れがあった。影響を受けるのは特定の拡張機能を有効にしている環境に限られる。

自動更新が有効な環境では、数日以内に順次適用される。手動で更新している場合は、設定画面から適用できる。

現時点で悪用の報告は出ていない。
WHY: 自動更新にしているなら何もしなくてよい。手動更新にしているなら、今日のうちに更新しておくと安心だ。
''',
    4: '''LEAD: 折りたたみ端末の新機種、決済は閉じたまま
POINTS:
- {title}を発表、来月発売
- 閉じたまま決済と通知確認が可能
- 価格は前機種と同じ
BODY:
{title}が発表された。閉じた状態の外側の画面が大きくなり、決済と通知の確認が開かずにできるようになった。発売は来月で、価格は前機種と同じに据え置かれた。

前機種からの変更点は外側画面の拡大と電池の増量が中心で、処理性能は据え置きである。折りたたみ端末は「開く手間」が敬遠される理由だったが、今回はその手間を減らす方向に振った。

| 項目 | 前機種 | 新機種 |
|---|---|---|
| 外側画面 | 3.6型 | 4.0型 |
| 電池 | 4,000mAh | 4,500mAh |
| 価格 | 据え置き | 据え置き |

反応は、外側画面の使い勝手を評価する声が多い一方、処理性能が据え置きな点を物足りないとする意見もある。
WHY: 開かずに済む操作が増えたので、決済や通知の確認が多い使い方なら乗り換えの価値がある。ゲームなど処理性能を重視するなら見送ってよい。
TERMS:
- 外側画面: 折りたたんだ状態で見える画面
''',
    5: '''{title}が発表された。発表は{date}付で、対象は開発者向けの提供枠にあたる。公開された情報の範囲では、機能の追加と提供条件の変更が同時に告知されている。

この動きが重要なのは、同じ領域の各社が提供条件をそろえはじめている点にある。価格と提供地域の差が縮まると、選定の基準は性能そのものより運用のしやすさへ移る。

反応は評価が割れている。
''',
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def url_for(source: str, day: int, index: int) -> str:
    return f"https://example.invalid/{source}/{day}/{index}"


def main() -> None:
    manifest: dict[str, dict] = {}

    # 1本だけ Atom 形式にして、RSS 2.0 と Atom の両対応を踏む
    atom_source = next(iter(CONFIG_SOURCES.get("ai") or [{}]), {}).get("id")

    for day, (date, rows) in DAYS.items():
        by_source: dict[str, list[tuple[str, str, int]]] = {}
        fulltext_of: dict[str, bool] = {}
        for i, (_legacy, cat, title) in enumerate(rows):
            source_def = assign(cat, i)
            source = str(source_def.get("id"))
            by_source.setdefault(source, []).append((title, date, i))
            fulltext_of[source] = bool(source_def.get("fulltext"))

        for source, entries in by_source.items():
            items = []
            for title, d, i in entries:
                link = url_for(source, day, i)
                display = ENGLISH.get(title, title)
                # config.yml で fulltext: true としたソースだけ全文を載せる
                # （AC-14 用）。設定と実態を一致させる。
                content = ""
                if fulltext_of.get(source):
                    content = (
                        "      <content:encoded><![CDATA[<p>"
                        + display
                        + "。発表の要点と背景をフィード側で全文配信している。</p>"
                        + "]]></content:encoded>\n"
                    )
                items.append(RSS_ITEM.format(
                    title=display, link=link,
                    pub=f"{d}T{7 + (i % 12):02d}:00:00+09:00", content=content))
            doc = (ATOM_DOC.format(source=source, items="".join(
                ATOM_ENTRY.format(title=ENGLISH.get(t, t), link=url_for(source, day, i),
                                  pub=f"{d}T{7 + (i % 12):02d}:00:00+09:00")
                for t, d, i in entries))
                if source == atom_source
                else RSS_DOC.format(source=source, items="".join(items)))
            write(FX / "rss" / f"day{day}" / f"{source}.xml", doc)

        # 写真集（type: gallery）のソース。media:thumbnail 付きの Atom
        for source_def in (s for group in CONFIG_SOURCES.values() for s in group
                           if s.get("type") == "gallery"):
            sid = str(source_def.get("id"))
            entries = "".join(GALLERY_ENTRY.format(
                title=t, link=f"https://example.invalid/{sid}/day{day}/{i}",
                pub=f"{date}T{6 + i:02d}:30:00+09:00",
                image=f"https://preview.redd.it/mock{day}{i:02d}.jpg?width=140&amp;height=140",
                text=("自作の分割配列。ケースは真鍮で重さ2kg。" if i % 2 else ""))
                for i, t in enumerate(GALLERY_TITLES))
            write(FX / "rss" / f"day{day}" / f"{sid}.xml", GALLERY_DOC.format(source=sid, entries=entries))

        # 収集0件シナリオ用の空フィード
        write(FX / "rss" / "empty" / "empty.xml",
              RSS_DOC.format(source="empty", items=""))

        # YouTubeチャンネルRSS。youtube.yml は未選定で空だが、`yt:videoId` から
        # URLを組み立てる経路はパーサーのテストで踏む。
        write(FX / "rss" / "youtube" / "sample_channel.xml",
              YT_DOC.format(pub=date))

        for i, (_legacy, cat, title) in enumerate(rows):
            source = str(assign(cat, i).get("id"))
            link = url_for(source, day, i)
            h = uhash(link)
            manifest[h] = {"url": link, "title": title, "category": cat,
                           "source": source, "day": day}

            # 本文HTML。意図的に一部を欠けさせて4段のフォールバックを踏ませる。
            #   index % 7 == 3 -> trafilatura 段が無い（jina が拾う）
            #   index % 11 == 5 -> jina も無い（firecrawl が拾う）
            #   index % 23 == 9 -> どの段も取れない（見出しとURLだけで残る）
            outlink = url_for("openai", 1, 0)  # 統合判定（outbound_links）を踏ませる
            page = PAGE.format(
                title=title,
                lead=f"{title}。{date}に公表された。",
                body="発表の詳細と、想定される影響についての説明が続く。",
                outlink=outlink)
            if i % 23 != 9:
                if i % 7 != 3:
                    write(FX / "pages" / "trafilatura" / f"{h}.html", page)
                if i % 11 != 5:
                    write(FX / "pages" / "jina" / f"{h}.html", page)
                write(FX / "pages" / "firecrawl" / f"{h}.html", page)

            # 反応。しきい値をまたぐ値を混ぜる（x_enabled 時の対象判定用）。
            # `after` は週次レビューで再取得したときの値。時間の経過で反応が
            # 伸びた状態を表し、変化の検出（FR-11）を踏めるようにする。
            hn_points = (i * 37) % 260
            hn_comments = (i * 13) % 190
            hatena = (i * 17) % 120
            reddit = (i * 7) % 140
            write(FX / "reactions" / f"{h}.json", json.dumps({
                "hn": {"hits": [{"points": hn_points,
                                 "num_comments": hn_comments}]},
                "hatena": hatena,
                "reddit": {"data": {"children": [{"data": {"score": reddit}}]}},
                "after": {
                    "hn": {"hits": [{"points": int(hn_points * 1.6) + 25,
                                     "num_comments": int(hn_comments * 1.5) + 15}]},
                    "hatena": int(hatena * 1.7) + 20,
                    "reddit": {"data": {"children": [
                        {"data": {"score": int(reddit * 1.5) + 18}}]}},
                },
            }, ensure_ascii=False, indent=2))

    write(FX / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    # Grok。citations が空のケースを必ず含める（AC-17）。
    write(FX / "grok" / "reactions.json", json.dumps({
        "default": {"summary": "性能より価格設定に議論が集まっている",
                    "citations": ["https://x.invalid/post/1",
                                  "https://x.invalid/post/2"]},
        "empty": {"summary": "評価が分かれている", "citations": []},
    }, ensure_ascii=False, indent=2))
    write(FX / "grok" / "topics.json", json.dumps({
        "topics": ["新モデルの価格改定", "オープンウェイトの採用",
                   "推論チップの供給", "国内規制の改訂"],
    }, ensure_ascii=False, indent=2))
    write(FX / "grok" / "discover.json", json.dumps({
        "items": [
            {"title": "公式ブログ 新機能の告知", "url": "https://example.invalid/discovered/a",
             "category": "ai"},
            {"title": "GitHub Releases 新バージョン", "url": "https://example.invalid/discovered/b",
             "category": "lang"},
            {"title": "arXiv 論文の公開", "url": "https://example.invalid/discovered/c",
             "category": "ai"},
        ],
    }, ensure_ascii=False, indent=2))
    write(FX / "search" / "results.json", json.dumps({
        "items": [
            {"title": "新興メディアの解説記事", "url": "https://newsite-a.invalid/post/1",
             "category": "ai"},
            {"title": "技術ブログの検証記事", "url": "https://newsite-b.invalid/post/2",
             "category": "devtools"},
            {"title": "企業の公式発表", "url": "https://newsite-a.invalid/post/3",
             "category": "industry"},
        ],
    }, ensure_ascii=False, indent=2))

    # 執筆モックが返す定型記事
    for n, text in ARTICLE_MDS.items():
        write(FX / "articles" / f"article{n}.md", text)

    write(FX / "releases" / "releases.json", json.dumps([
        {"name": "1.90.0", "tag_name": "1.90.0",
         "html_url": "https://example.invalid/releases/rust-1.90.0",
         "published_at": "2026-09-01T10:00:00+09:00",
         "body": "安定版のリリース。互換性のある変更を含む。"},
        {"name": "3.14.0", "tag_name": "v3.14.0",
         "html_url": "https://example.invalid/releases/cpython-3.14.0",
         "published_at": "2026-09-01T11:00:00+09:00",
         "body": "新しい構文と標準ライブラリの更新。"},
    ], ensure_ascii=False, indent=2))

    write(FX / "stooq" / "quotes.csv",
          "Symbol,Date,Open,High,Low,Close,Volume\n"
          "NVDA,2026-09-01,180.00,186.20,179.10,183.78,41200000\n"
          "MSFT,2026-09-01,432.10,437.00,430.50,435.90,18300000\n"
          "AAPL,2026-09-01,228.40,230.10,226.80,227.15,35100000\n"
          "GOOGL,2026-09-01,201.30,204.90,200.80,204.11,22400000\n"
          "AMD,2026-09-01,142.60,145.30,141.90,144.72,31800000\n"
          "^SPX,2026-09-01,5720.00,5758.40,5711.20,5749.63,0\n")

    # Slack のボタン押下ペイロード（AC-26 / AC-27 / AC-28 の入力）
    write(FX / "slack" / "block_actions.json", json.dumps({
        "type": "block_actions",
        "user": {"id": "U0000001"},
        "channel": {"id": "C0000001"},
        "message": {"ts": "1756000000.000100"},
        "actions": [{"action_id": "create_articles", "type": "button"}],
        "state": {"values": {
            "cat_ai_0": {"stories_ai_0": {"type": "checkboxes", "selected_options": [
                {"value": "s_000001"}, {"value": "s_000002"}, {"value": "s_000003"},
                {"value": "s_000004"}, {"value": "s_000005"}, {"value": "s_000006"},
                {"value": "s_000007"}, {"value": "s_000008"},
            ]}},
            "cat_lang_0": {"stories_lang_0": {"type": "checkboxes",
                                              "selected_options": []}},
        }},
    }, ensure_ascii=False, indent=2))

    # シナリオ定義
    write(FX / "scenarios.yml", """# 3日間の状態変化を表すシナリオ定義。
# mock クライアントはこの定義を見て、日ごとに返すフィクスチャを切り替える。

normal:
  description: 通常系。3日分を順に流す
  days: [1, 2, 3]
  feeds: {1: day1, 2: day2, 3: day3}

empty:
  description: 収集0件。探索（FR-2）の段階1から順に走る
  days: [1]
  feeds: {1: empty}

partial_outage:
  description: 部分障害。株価と執筆モデルが3回とも失敗する
  days: [1]
  feeds: {1: day1}
  fail: "stooq,claude"

carryover:
  description: 繰り越し。1日目の ai が18本で上限12件を超え、6件が翌日へ残る
  days: [1, 2]
  feeds: {1: day1, 2: day2}

merge:
  description: 既存話題への統合。2日目の続報が1日目の繰り越し話題へ入る
  days: [1, 2]
  feeds: {1: day1, 2: day2}

written_excluded:
  description: 記事化済みの除外。2日目に記事化した話題が3日目に再処理されない
  days: [1, 2, 3]
  feeds: {1: day1, 2: day2, 3: day3}
  click_on_day: 2
""")

    print("fixtures written to", FX)
    print("articles:", len(manifest))


if __name__ == "__main__":
    main()
