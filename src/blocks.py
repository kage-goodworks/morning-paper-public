"""Slack のブロック組み立て。

**外部I/Oを持たない純粋関数だけ（NFR-1）。**Slack へ投げるのは `src/slack.py`。
出力した JSON はそのまま Block Kit Builder へ貼って見た目を確認できる。

分割の単位は「ブロック」ではなく**カテゴリのかたまり**にしてある。見出しと
チェックボックスが別のメッセージへ離れると、どのカテゴリの話題か分からなく
なるため。1カテゴリだけで上限を超える場合は見出しを繰り返す。
"""

from __future__ import annotations

from typing import Any

from . import safety
from .models import Quote, Story

WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
BUTTON_ACTION_ID = "create_articles"
#: 一括選択のボタン。押下は Workers（workers/index.js）が受けて、同じ一覧を
#: `initial_options` を付け替えて書き戻す。Actions は起動しない
SELECT_ALL_ACTION_ID = "select_all"
CLEAR_ALL_ACTION_ID = "clear_all"
SELECT_CATEGORY_ACTION_ID = "select_category"

#: 番号の表記。`1.` を題名の前に置くと題名と一体に見えて読みにくいため、
#: 丸数字で1文字にして題名と切り離す（本人の要望、2026-10-01）
_CIRCLED = ("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
            "㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿")


def circled(number: int) -> str:
    """1〜50 は丸数字、それ以外は `n.`。"""
    if 1 <= number <= len(_CIRCLED):
        return _CIRCLED[number - 1]
    return f"{number}."


def weekday_label(iso_date: str) -> str:
    from datetime import date
    try:
        return WEEKDAYS[date.fromisoformat(iso_date).weekday()]
    except ValueError:
        return ""


def story_label(story: Story) -> str:
    """一覧のラベル。`<最新記事URL|タイトル>`。押すと最新記事の元ページが開く。

    記事が複数ある話題でも「（n件）」は付けない（本人の要望、2026-10-01。
    以前は AC-6 で付けていた）。件数は紙面の「経過」で分かる。
    """
    url = story.latest_url or ""
    # タイトルは外部由来。`>` を素通しすると、そこでリンクが終わって
    # 続きが別のリンクとして描画される（任意リンクの差し込み）。
    title = safety.slack_escape(story.title)
    if not url or not safety.is_fetchable(url):
        return title
    return f"<{url}|{title}>"


#: Slack Block Kit の文字数上限。超えると chat.postMessage が invalid_blocks
#: を返し、一覧そのものが届かなくなる。ここで必ず収める。
OPTION_TEXT_MAX = 75
CONTEXT_TEXT_MAX = 2000
HEADER_TEXT_MAX = 150
SECTION_TEXT_MAX = 3000


def _clip(text: str, limit: int) -> str:
    """上限に収める。切ったことが分かるよう末尾に「…」を付ける。"""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


#: 記事化済みの印。元の位置に残したまま「もうやった」と分かるようにする
DONE_MARK = "✅ "


def option_label(story: Story, number: int, *, done: bool = False) -> str:
    """チェックボックスの文字列。`① タイトル`。済みは `✅ ① …`。

    **リンクは入れない。**上限が75文字で、本物のURLは単体で60〜120文字
    あるため `<URL|題名>` は物理的に収まらない。リンクはカテゴリ見出しの
    下に `link_marks` で番号を並べ、番号で対応づける（AC-5）。
    「（n件）」は付けない（本人の要望、2026-10-01）。
    """
    prefix = (DONE_MARK if done else "") + circled(number) + " "
    room = OPTION_TEXT_MAX - len(prefix)
    title = _clip(safety.slack_escape(story.title), max(1, room))
    return prefix + title


def link_marks(stories: list[Story], start: int = 1,
               written: dict[str, str] | None = None) -> str:
    """番号リンクの列。`① ② …` を押すと最新記事の元ページが開く。

    記事化済みの話題には紙面のスレッドへの `📰` を添える（permalink が
    あるときだけ。無ければ壊れたリンクを作らない）。
    """
    written = written or {}
    marks: list[str] = []
    for offset, story in enumerate(stories):
        number = start + offset
        url = story.latest_url or ""
        mark = circled(number)
        if url and safety.is_fetchable(url):
            mark = f"<{url}|{mark}>"
        permalink = written.get(story.id) or ""
        if story.id in written and permalink and safety.is_fetchable(permalink):
            mark += f"<{permalink}|📰>"
        marks.append(mark)
    return "↗ " + " ".join(marks)


def _option(story: Story, number: int, *, done: bool = False) -> dict[str, Any]:
    return {"text": {"type": "mrkdwn",
                     "text": option_label(story, number, done=done)},
            "value": story.id}


def checkbox_elements(category: str, stories: list[Story], *,
                      options_max: int = 10,
                      selected: set[str] | None = None,
                      written: dict[str, str] | None = None
                      ) -> list[dict[str, Any]]:
    """1カテゴリ分のブロックを作る。

    Slack の制限で1要素あたり `options_max`（10）件までなので、超える分は
    同じカテゴリ内で要素を分ける（AC-3）。

    記事化済み（`written`）の話題は**チェックボックスの外に出し**、同じ
    位置に `✅ n. タイトル` の文字として置く（AC-28）。チェックボックスの
    中に「押せない項目」は作れないため、済みの行の前後で要素を区切る。
    並び順と番号は変わらない。
    """
    selected = selected or set()
    written = written or {}
    out: list[dict[str, Any]] = []
    part = 0
    pending_options: list[tuple[dict[str, Any], Story]] = []
    done_lines: list[str] = []

    def flush_options() -> None:
        nonlocal part
        if not pending_options:
            return
        options = [o for o, _ in pending_options]
        element: dict[str, Any] = {
            "type": "checkboxes",
            "action_id": f"stories_{category}_{part}",
            "options": options,
        }
        initial = [o for o, story in pending_options if story.id in selected]
        if initial:
            element["initial_options"] = initial
        out.append({"type": "actions",
                    "block_id": f"cat_{category}_{part}",
                    "elements": [element]})
        part += 1
        pending_options.clear()

    def flush_done() -> None:
        if done_lines:
            out.append(_context("\n".join(done_lines)))
            done_lines.clear()

    for index, story in enumerate(stories, start=1):
        if story.id in written:
            flush_options()
            done_lines.append(option_label(story, index, done=True))
            continue
        flush_done()
        pending_options.append((_option(story, index), story))
        if len(pending_options) >= options_max:
            flush_options()
    flush_options()
    flush_done()
    return out


#: 見出しブロックは1カテゴリ2つ（`header` ＋ 番号リンクの `section`）
HEADING_BLOCKS = 2


def category_heading(label: str, stories: list[Story], *,
                     category: str = "",
                     written: dict[str, str] | None = None,
                     suffix: str = "", part: int = 0,
                     with_select: bool = True) -> list[dict[str, Any]]:
    """カテゴリの見出し。2ブロック。

    1. `header`（大きな文字）にカテゴリ名。題名の列と見分けがつくように
       （本人の要望、2026-10-01。以前は太字の `section` 1つだった）
    2. `section` に番号リンクを1行。右端に「この区分を全選択」ボタン。
       リンクをチェックボックスの合間に挟むと、どこがカテゴリの切れ目か
       分かりにくくなるため、見出しの下に全件まとめる

    `part` は同じカテゴリの見出しを繰り返すときの通し番号。block_id は
    1通の中で一意でなければならない。
    """
    key = category or label
    tag = f"{key}_{part}" if part else key
    links = _section(link_marks(stories, start=1, written=written))
    links["block_id"] = f"links_{tag}"
    remaining = [s for s in stories if s.id not in (written or {})]
    if with_select and remaining:
        links["accessory"] = {
            "type": "button", "action_id": SELECT_CATEGORY_ACTION_ID,
            "text": {"type": "plain_text", "text": "この区分を全選択"},
            "value": key,
        }
    header = _header(label + suffix)
    header["block_id"] = f"head_{tag}"
    return [header, links]


def _section(text: str) -> dict[str, Any]:
    return {"type": "section",
            "text": {"type": "mrkdwn", "text": _clip(text, SECTION_TEXT_MAX)}}


def _header(text: str) -> dict[str, Any]:
    return {"type": "header",
            "text": {"type": "plain_text",
                     "text": _clip(text, HEADER_TEXT_MAX)}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": _clip(text, CONTEXT_TEXT_MAX)}]}


def _status_line(status: list[str]) -> str:
    return "取得状況: " + (" / ".join(status) if status else "すべて取得できました")


def _button(date_str: str) -> dict[str, Any]:
    """末尾のボタン列。「記事を作成」と、一括選択の2つ。"""
    return {
        "type": "actions", "block_id": "create",
        "elements": [{
            "type": "button", "style": "primary", "action_id": BUTTON_ACTION_ID,
            "text": {"type": "plain_text", "text": "記事を作成"},
            "value": date_str,
        }, {
            "type": "button", "action_id": SELECT_ALL_ACTION_ID,
            "text": {"type": "plain_text", "text": "すべて選択"},
            "value": date_str,
        }, {
            "type": "button", "action_id": CLEAR_ALL_ACTION_ID,
            "text": {"type": "plain_text", "text": "選択を解除"},
            "value": date_str,
        }],
    }


# --------------------------------------------------------------------------
# 分割
# --------------------------------------------------------------------------

def split_groups(groups: list[list[dict[str, Any]]], *, head: list[dict],
                 tail: list[dict], limit: int) -> list[list[dict[str, Any]]]:
    """かたまり単位でメッセージへ詰める。

    かたまり（カテゴリ見出し＋そのチェックボックス）を途中で割らない。
    末尾（ボタンを含む）は最後の1通にだけ付ける（AC-4）。
    """
    messages: list[list[dict[str, Any]]] = []
    current = list(head)

    for group in groups:
        if len(current) + len(group) > limit and len(current) > len(head):
            messages.append(current)
            current = []
        current.extend(group)

    if len(current) + len(tail) > limit and current:
        messages.append(current)
        current = []
    current.extend(tail)
    messages.append(current)
    return messages


def category_groups(shown: dict[str, list[Story]], labels: dict[str, str], *,
                    options_max: int, limit: int,
                    selected: set[str] | None = None,
                    written: dict[str, str] | None = None
                    ) -> list[list[dict[str, Any]]]:
    """カテゴリごとのかたまりを作る。

    1カテゴリだけで上限を超える場合は、見出しを繰り返して分ける。
    """
    groups: list[list[dict[str, Any]]] = []
    for category, stories in shown.items():
        if not stories:
            continue
        label = labels.get(category, category)
        elements = checkbox_elements(category, stories,
                                     options_max=options_max, selected=selected,
                                     written=written)
        group = category_heading(label, stories, category=category,
                                 written=written) + elements
        if len(group) <= limit or limit <= HEADING_BLOCKS:
            groups.append(group)
            continue
        # 1カテゴリで上限を超える。見出しを繰り返して割る。
        per = max(1, limit - HEADING_BLOCKS)
        for part, index in enumerate(range(0, len(elements), per)):
            chunk = elements[index:index + per]
            suffix = "" if index == 0 else "（続き）"
            groups.append(category_heading(label, stories, category=category,
                                           written=written, suffix=suffix,
                                           part=part) + chunk)
    return groups


# --------------------------------------------------------------------------
# 一覧
# --------------------------------------------------------------------------

def build_list(*, date_str: str, shown: dict[str, list[Story]],
               labels: dict[str, str], status: list[str],
               quotes: list[Quote] | None = None,
               x_topics: list[str] | None = None,
               options_max: int = 10, blocks_per_message: int = 45,
               selected: set[str] | None = None) -> list[list[dict[str, Any]]]:
    """一覧のブロックを組み立て、メッセージ単位に分割して返す。"""
    total = sum(len(v) for v in shown.values())
    head = [
        _header(f"{date_str}（{weekday_label(date_str)}）のニュース 話題 {total}件"),
        _context(_status_line(status)),
        {"type": "divider"},
    ]
    # かたまりは「冒頭ブロックを置いたあとの残り」に収まる大きさで作る。
    groups = category_groups(shown, labels, options_max=options_max,
                             limit=blocks_per_message - len(head),
                             selected=selected)
    tail = _tail(date_str, quotes, x_topics, with_button=True)
    return split_groups(groups, head=head, tail=tail, limit=blocks_per_message)


def _tail(date_str: str, quotes: list[Quote] | None,
          x_topics: list[str] | None, *, with_button: bool) -> list[dict]:
    tail: list[dict[str, Any]] = [{"type": "divider"}]
    if x_topics:
        tail.append(_section("*昨日Xで話題になったこと*"))
        tail.append(_context("・" + " ・".join(x_topics)))
    if quotes:
        tail.append(_section("*株価*  " + " / ".join(
            f"{q.symbol} {q.change_pct:+.1f}%" for q in quotes)))
    if with_button:
        tail.append(_button(date_str))
    return tail


# --------------------------------------------------------------------------
# 記事化後の chat.update
# --------------------------------------------------------------------------

def build_update(*, date_str: str, shown: dict[str, list[Story]],
                 labels: dict[str, str], status: list[str],
                 written: dict[str, str], pending: set[str] | None = None,
                 quotes: list[Quote] | None = None,
                 x_topics: list[str] | None = None,
                 options_max: int = 10, blocks_per_message: int = 45,
                 ) -> list[list[dict[str, Any]]]:
    """記事化後の一覧（AC-28）。

    済みの話題を**元の位置に残したまま** `✅` を付けてチェック固定で出す。
    一覧から消したり下へ寄せたりすると、番号がずれて見比べられなくなる。
    保留（`pending`）の話題はチェックが付いたまま復元する（AC-26）。

    **一覧が複数通に分かれていた場合に備えて、こちらも複数通を返す。**
    切り捨てると話題が黙って消えるため、上限で切らずに分割する。
    """
    pending = pending or set()
    total = sum(len(v) for v in shown.values())
    remaining = sum(1 for v in shown.values() for s in v if s.id not in written)
    head = [
        _header(f"{date_str}（{weekday_label(date_str)}）のニュース 話題 {total}件"),
        _context(_status_line(status)),
        {"type": "divider"},
    ]
    groups = category_groups(shown, labels, options_max=options_max,
                             limit=blocks_per_message - len(head),
                             selected=pending, written=written)
    tail = _tail(date_str, quotes, x_topics, with_button=bool(remaining))
    return split_groups(groups, head=head, tail=tail, limit=blocks_per_message)


def build_exhausted(date_str: str, note_text: str) -> list[dict[str, Any]]:
    """一覧が2通目以降まであったが、更新後は不要になった分の差し替え。"""
    return [
        _header(f"{date_str} のニュース"),
        _context(note_text),
    ]


# --------------------------------------------------------------------------
# ボタン押下ペイロードの読み取り
# --------------------------------------------------------------------------

def selected_story_ids(payload: dict[str, Any]) -> list[str]:
    """`state.values` からチェックされた全 story_id を取り出す。

    Workers 側と Actions 側の双方から使う。純粋関数。
    """
    out: list[str] = []
    values = ((payload or {}).get("state") or {}).get("values") or {}
    for block in values.values():
        for element in (block or {}).values():
            if (element or {}).get("type") != "checkboxes":
                continue
            for option in element.get("selected_options") or []:
                value = option.get("value")
                if value and value not in out:
                    out.append(value)
    return out
