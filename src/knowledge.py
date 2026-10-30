"""記事化した内容を、主題ごとの知識ノートとして Vault に積み上げる。

本人の要望（2026-10-15）: 記事そのものではなく「中身の知識」を、主題ごとに
分類して増やしていく。忙しくて読めない日でも、どの AI からでも「ここ3日の
ニュースを要約して」「〇〇について最新の動きは」に答えられるようにする。

置き場所（`config.yml` の `knowledge.dir`、既定 `40_Knowledge/Tech`）:

| パス | 中身 |
|---|---|
| `<dir>/<分野>/<主題>.md` | 1主題1ノート。新しい知識を日付つきで**上に追記**していく |
| `<dir>/_INDEX.md` | 入口。「最近の追加（日付順）」と「主題一覧」 |

主題名と知識は、記事を書くのと**同じ呼び出し**で書かせる（`TOPIC:` と
`KNOW:` の部品。`write.build_prompt`）。LLM を使う箇所は増やさない。
部品が欠けた記事（降格先の出力など）は、要点と「自分との関係」を使い、
その分野の「未分類」ノートへ入れる。

同じ話題を2度記録しない。各項目に `<!-- story:<id> -->` の印を付け、
作り直し（force）で同じ話題が来ても追記しない。

純粋関数（組み立て・解析）と、Vault への読み書き（`record`）を分ける。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import safety
from .clients.parsers import article_sections

DEFAULT_DIR = "40_Knowledge/Tech"
INDEX_NAME = "_INDEX.md"
UNSORTED = "未分類"
#: 索引の「最近の追加」に残す日数
DEFAULT_RECENT_DAYS = 30
#: 執筆プロンプトに渡す既存の主題名の上限（多すぎると指示が埋もれる）
TOPICS_IN_PROMPT = 300

_FORBIDDEN = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')
_MARK = re.compile(r"<!-- story:([A-Za-z0-9_-]+) -->")
_RECENT_LINE = re.compile(
    r"^- (\d{4}-\d{2}-\d{2}) \[\[([^\]|]+)\|([^\]]+)\]\] — (.*)$")
_TOPIC_LINE = re.compile(
    r"^- \[\[([^\]|]+)\|([^\]]+)\]\]（最終 (\d{4}-\d{2}-\d{2})・(\d+)件）$")
_SECTION_HEAD = re.compile(r"^### (.+)$")


# --------------------------------------------------------------------------
# 名前と場所
# --------------------------------------------------------------------------

def topic_key(name: str) -> str:
    """主題名の比較キー。全角半角・大小・空白の違いを無視する。純粋関数。"""
    text = unicodedata.normalize("NFKC", name or "").casefold()
    return re.sub(r"\s+", "", text)


def clean_topic(name: str) -> str:
    """主題名を整える。ファイル名に使えない文字を落とし、長さを抑える。"""
    text = unicodedata.normalize("NFKC", (name or "").strip())
    text = _FORBIDDEN.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text[:60].strip() or UNSORTED


def note_path(base: str, category: str, topic: str) -> str:
    """主題ノートの Vault 内パス。純粋関数。"""
    cat = safety.safe_segment(category or "other") or "other"
    return f"{base}/{cat}/{clean_topic(topic)}.md"


def link_target(rel_path: str) -> str:
    """Obsidian のリンク先（拡張子を落とす）。"""
    return rel_path[:-3] if rel_path.endswith(".md") else rel_path


# --------------------------------------------------------------------------
# 1件分の知識
# --------------------------------------------------------------------------

@dataclass
class Entry:
    """主題ノートへ足す1項目。"""
    story_id: str
    date: str
    category: str
    topic: str
    headline: str
    facts: list[str] = field(default_factory=list)
    use: str = ""
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # 元の記事ノート（Vault内パス）


def entry_from(*, story_id: str, title: str, category: str, body_md: str,
               sources: list[str], notes: list[str], date_str: str) -> Entry:
    """執筆結果の1本から知識の1項目を作る。純粋関数。

    `TOPIC` と `KNOW` があればそれを使う。無ければ要点（POINTS）を知識にし、
    主題は「未分類」にする（その分野の未分類ノートへ入る）。
    """
    sec = article_sections.parse(body_md)
    facts = [f for f in (sec.knowledge or sec.points) if f.strip()]
    if not facts and sec.lead:
        facts = [sec.lead]
    return Entry(
        story_id=story_id, date=date_str, category=category,
        topic=clean_topic(sec.topic) if sec.topic else UNSORTED,
        headline=(sec.lead or title).strip(),
        facts=facts[:6],
        use=" ".join(sec.why.split())[:600],
        sources=[u for u in sources if safety.is_fetchable(u)][:4],
        notes=list(notes)[:4])


def render_entry(entry: Entry) -> str:
    """主題ノートに足す1項目の Markdown。純粋関数。"""
    lines = [f"## {entry.date} {_one_line(entry.headline)}",
             f"<!-- story:{story_mark(entry.story_id)} -->", ""]
    lines.extend(f"- {_one_line(f)}" for f in entry.facts)
    if entry.use:
        lines.extend(["", f"**活かし方**: {_one_line(entry.use)}"])
    refs: list[str] = []
    refs.extend(f"[[{link_target(n)}|元記事]]" for n in entry.notes)
    refs.extend(safety.markdown_link(_host(u), u) for u in entry.sources)
    if refs:
        lines.extend(["", "出典: " + " / ".join(refs)])
    return "\n".join(lines) + "\n"


def story_mark(story_id: str) -> str:
    """項目の印に使う話題の id。英数字と `_` `-` だけ残す。純粋関数。"""
    return re.sub(r"[^A-Za-z0-9_-]", "", story_id or "")[:40] or "unknown"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else "出典"


# --------------------------------------------------------------------------
# 主題ノート
# --------------------------------------------------------------------------

def new_note(*, topic: str, category: str, label: str, date_str: str) -> str:
    """まだ無い主題ノートの頭。純粋関数。"""
    digest = hashlib.sha256(f"{category}/{topic_key(topic)}".encode("utf-8")).hexdigest()[:8]
    tag = "topic/ai" if category == "ai" else "topic/development"
    return "\n".join([
        "---",
        f"id: knowledge-{safety.safe_segment(category) or 'other'}-{digest}",
        "type: reference",
        "status: active",
        "vault: open",
        f"created: {date_str}",
        f"updated: {date_str}",
        "source: automation",
        "agents:",
        "  - claude",
        "tags:",
        f"  - {tag}",
        "  - workflow/automation",
        f"summary: {safety.yaml_scalar(f'{topic}について朝刊で集めた最新の知識（新しい順に追記）')}",
        f"topic: {safety.yaml_scalar(topic)}",
        f"category: {safety.safe_segment(category) or 'other'}",
        "---",
        "",
        f"# {topic}",
        "",
        f"分野: {label}。朝刊（morning-paper）で記事化した話題から、この主題について"
        "分かったことを新しい順に積み上げる。各項目の出典から元の記事を辿れる。",
        "",
    ])


def add_entry(text: str, entry: Entry) -> tuple[str, bool]:
    """主題ノートに1項目を足す。すでに同じ話題があれば足さない。純粋関数。

    新しい項目は**一番上**（最初の `## ` の前）に入れる。最新を先に読めるように。
    """
    if story_mark(entry.story_id) in set(_MARK.findall(text)):
        return text, False
    block = render_entry(entry)
    lines = text.split("\n")
    at = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    head = "\n".join(lines[:at]).rstrip("\n")
    rest = "\n".join(lines[at:]).lstrip("\n")
    merged = head + "\n\n" + block + ("\n" + rest if rest else "")
    merged = re.sub(r"^updated: .*$", f"updated: {entry.date}", merged, count=1, flags=re.M)
    return merged.rstrip("\n") + "\n", True


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------

@dataclass
class Topic:
    name: str
    path: str
    category: str
    updated: str
    count: int


@dataclass
class Recent:
    date: str
    path: str
    name: str
    headline: str


@dataclass
class Index:
    topics: dict[str, Topic] = field(default_factory=dict)     # topic_key -> Topic
    recent: list[Recent] = field(default_factory=list)

    def find(self, name: str) -> Topic | None:
        return self.topics.get(topic_key(name))


def parse_index(text: str, *, base: str) -> Index:
    """索引ノートを読む。形が崩れていても読める行だけ拾う。純粋関数。"""
    index = Index()
    section = ""
    for line in (text or "").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = _RECENT_LINE.match(line)
        if m and section.startswith("最近"):
            index.recent.append(Recent(date=m.group(1), path=m.group(2) + ".md",
                                       name=m.group(3), headline=m.group(4)))
            continue
        m = _TOPIC_LINE.match(line)
        if m:
            path = m.group(1) + ".md"
            parts = path[len(base) + 1:].split("/") if path.startswith(base + "/") else []
            category = parts[0] if len(parts) == 2 else "other"
            topic = Topic(name=m.group(2), path=path, category=category,
                          updated=m.group(3), count=int(m.group(4)))
            index.topics[topic_key(topic.name)] = topic
    return index


def render_index(index: Index, *, labels: dict[str, str], order: list[str],
                 today: str, recent_days: int = DEFAULT_RECENT_DAYS) -> str:
    """索引ノートを組み立てる。純粋関数。"""
    try:
        cutoff = (date.fromisoformat(today) - timedelta(days=recent_days)).isoformat()
    except ValueError:
        cutoff = ""
    recent = sorted((r for r in index.recent if r.date >= cutoff),
                    key=lambda r: r.date, reverse=True)
    lines = [
        "---",
        "id: knowledge-tech-index",
        "type: reference",
        "status: active",
        "vault: open",
        f"created: {today}",
        f"updated: {today}",
        "source: automation",
        "agents:",
        "  - claude",
        "tags:",
        "  - topic/ai",
        "  - workflow/automation",
        "summary: 朝刊で集めた技術ニュースの知識の入口（最近の追加と主題一覧）",
        "---",
        "",
        "# 技術ニュースの知識（索引）",
        "",
        "朝刊（morning-paper）で記事化した話題を、記事ではなく**主題ごとの知識**として積み上げている。",
        "",
        "AIへの案内:",
        "- 「ここN日のニュース」「最近の動き」を聞かれたら、下の「最近の追加」を日付で絞り、"
        "リンク先の主題ノートの該当日の項目を読んで答える。",
        "- 特定の技術・製品・会社について聞かれたら、「主題一覧」から主題ノートを開く。"
        "項目は新しい順。各項目の「活かし方」は本人の開発への関係。",
        "- 元の記事全文が要るときは、各項目の出典の「元記事」リンク（`40_Knowledge/News/`）を辿る。",
        "",
        f"## 最近の追加（直近{recent_days}日）",
        "",
    ]
    lines.extend(f"- {r.date} [[{link_target(r.path)}|{r.name}]] — {_one_line(r.headline)}"
                 for r in recent)
    if not recent:
        lines.append("（まだありません）")
    lines.extend(["", "## 主題一覧", ""])
    by_cat: dict[str, list[Topic]] = {}
    for topic in index.topics.values():
        by_cat.setdefault(topic.category, []).append(topic)
    cats = [c for c in order if c in by_cat] + sorted(c for c in by_cat if c not in order)
    for cat in cats:
        lines.extend([f"### {labels.get(cat, cat)}", ""])
        for topic in sorted(by_cat[cat], key=lambda t: (t.updated, t.count), reverse=True):
            lines.append(f"- [[{link_target(topic.path)}|{topic.name}]]"
                         f"（最終 {topic.updated}・{topic.count}件）")
        lines.append("")
    if not cats:
        lines.append("（まだありません）")
    return "\n".join(lines).rstrip("\n") + "\n"


def topic_names(index: Index, limit: int = TOPICS_IN_PROMPT) -> list[str]:
    """執筆プロンプトに渡す既存の主題名。新しく使われたものから。純粋関数。"""
    ordered = sorted(index.topics.values(), key=lambda t: (t.updated, t.count), reverse=True)
    return [t.name for t in ordered if t.name != UNSORTED][:limit]


# --------------------------------------------------------------------------
# Vault への読み書き
# --------------------------------------------------------------------------

def base_dir(config) -> str:
    return str(config.get("knowledge.dir", DEFAULT_DIR) or DEFAULT_DIR).strip("/")


def enabled(config) -> bool:
    return bool(config.get("knowledge.enabled", True))


def load_index(run) -> Index:
    """索引ノートを読む。読めなければ空（主題ノート自体はパスで突き合わせる）。"""
    base = base_dir(run.config)
    text = run.guarded(lambda: run.clients.notes.read(f"{base}/{INDEX_NAME}"),
                       target="Open Vault（知識の索引の読み出し）", impact="",
                       default=None, status="")
    return parse_index(text or "", base=base)


def record(run, entries: list[Entry]) -> int:
    """知識の項目を主題ノートへ足し、索引を書き直す。足した件数を返す。

    commit は呼び出し側（`vault.commit`）でまとめて行う。失敗しても止めない
    （記事は Slack に届いている。知識は次の記事化でまた増える）。
    """
    from . import vault

    if not entries:
        return 0
    config = run.config
    base = base_dir(config)
    labels = dict(config.get("categories.labels", {}) or {})
    order = list(config.get("categories.order", []) or [])
    recent_days = int(config.get("knowledge.recent_days", DEFAULT_RECENT_DAYS))
    index = load_index(run)

    added = 0
    for entry in entries:
        known = index.find(entry.topic)
        # 既にある主題は、その主題の分野に入れる（同じ主題が分野をまたいで2つにならない）
        category = known.category if known else entry.category
        name = known.name if known else entry.topic
        rel = known.path if known else note_path(base, category, name)
        current = run.guarded(lambda rel=rel: run.clients.notes.read(rel),
                              target="Open Vault（知識ノートの読み出し）", impact="",
                              default=None, status="")
        text = current or new_note(topic=name, category=category,
                                   label=labels.get(category, category), date_str=entry.date)
        merged, changed = add_entry(text, entry)
        if not changed or not vault.write_note(run, rel, merged):
            continue
        added += 1
        topic = index.topics.get(topic_key(name)) or Topic(
            name=name, path=rel, category=category, updated=entry.date, count=0)
        topic.updated = max(topic.updated, entry.date)
        topic.count += 1
        index.topics[topic_key(name)] = topic
        index.recent.append(Recent(date=entry.date, path=rel, name=name,
                                   headline=entry.headline))

    if added:
        today = max(e.date for e in entries)
        vault.write_note(run, f"{base}/{INDEX_NAME}",
                         render_index(index, labels=labels, order=order,
                                      today=today, recent_days=recent_days))
        run.note_status(f"知識: {added}件を主題ノートへ追記しました（{base}）")
    return added
