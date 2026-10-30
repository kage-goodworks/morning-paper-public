"""実行の記録の読み書きと状態遷移。

**状態遷移そのものはモックしない。**モック実行でも本番と同じここを通る。
モックでは書き先が `out/state/` になるだけ。

## 書き手ごとにファイルを分ける

3つのワークフロー（朝・記事化・週次）はどれもGitHub Actionsで走り、記録を
commitして持ち回す。1つのファイルに全部を書いていると、**同時に動いたとき
あとから書いたほうが前の記録を消す。**朝が長引いて週次と重なる、記事化の
ボタンが朝の最中に押される、といったことは普通に起きる。

そこで書き手ごとにファイルを分け、衝突する面を無くす。

| ファイル | 中身 | 書く人 |
|---|---|---|
| `state/collect.json` | last_success_at / seen / title_norm | 朝だけ |
| `state/stories.json` | stories / story_urls / story_articles / story_notes / listed_messages | 朝だけ |
| `state/written.json` | written / written_articles | 記事化だけ |
| `state/outbox.json` | pending（Slackへ送れなかった分） | 3つとも |

`outbox.json` だけは3つが触るが、中身は送り直す待ち行列なので、
最悪1件取りこぼしても次回の投稿が1回遅れるだけで済む。

`stories` の `status` は carryover → listed → written と進む。**記事化は
`stories` を書き換えず** `written.json` にだけ記録する。こうすると記事化が
朝のファイルへ触らずに済む。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Story


@dataclass
class State:
    last_success_at: str | None = None
    #: ソースごとの前回成功時点。落ちていたソースは復旧後、その間の記事も
    #: 拾えるように、そのソースだけ古い時点から対象にする
    source_success_at: dict[str, str] = field(default_factory=dict)
    seen: dict[str, str] = field(default_factory=dict)
    title_norm: dict[str, str] = field(default_factory=dict)
    stories: dict[str, dict] = field(default_factory=dict)
    written: dict[str, dict] = field(default_factory=dict)
    pending: list[dict] = field(default_factory=list)
    #: 話題ごとの記事URL。統合判定（outbound_links）で使う。
    story_urls: dict[str, list[str]] = field(default_factory=dict)
    #: 話題ごとの記事id。記事化のときに読み出す。
    story_articles: dict[str, list[str]] = field(default_factory=dict)
    #: 話題ごとの記事ノートのパス。記事化のときにVaultから読む先。
    story_notes: dict[str, list[str]] = field(default_factory=dict)
    #: その日の一覧を投稿したメッセージのts。分割された場合は複数入る。
    #: 記事化後の chat.update は**全通に対して**行う必要がある。
    listed_messages: dict[str, list[str]] = field(default_factory=dict)
    #: 週次レビューを投稿した週（"2026-W38" → 投稿の ts）。手動実行や再実行で
    #: 同じ週を二度送らないため
    weekly_posted: dict[str, str] = field(default_factory=dict)
    #: Slack には送れたが Vault に保存できなかった週のノート本文。次回の
    #: 週次で（Claude を呼び直さずに）保存だけやり直し、成功したら消す
    weekly_unsaved: dict[str, str] = field(default_factory=dict)
    #: 話題ごとに「どの記事まで執筆に使ったか」。続報のときは、ここに無い
    #: 記事だけを渡す。前回書いた文章を保存していなくても、新しい情報だけの
    #: 記事になる。
    written_articles: dict[str, list[str]] = field(default_factory=dict)
    #: 自動でやり直す記事化の待ち行列（2026-10-24）。Slack に送れなかった・
    #: AI が応答しなかった・時間切れ・途中で止まった話題を、本人が押し直さなくても
    #: 後の実行で作り直して送る。{date, story_ids, attempts, reason, since}
    redo: list[dict] = field(default_factory=list)
    #: GitHub Actions の利用時間の見積り（分）。月ごと。無料枠を使い切ると朝の処理
    #: すら動かなくなるため、近づいたら重い処理（作り直し）を控える（2026-10-30）
    usage: dict[str, float] = field(default_factory=dict)
    #: 利用時間の警告を出した月（同じ月に何度も出さない）
    usage_warned: dict[str, str] = field(default_factory=dict)

    # -- 話題 ---------------------------------------------------------------

    def carryover_stories(self) -> list[Story]:
        return [Story.from_dict(s) for s in self.stories.values()
                if s.get("status") == "carryover"]

    def all_stories(self) -> list[Story]:
        return [Story.from_dict(s) for s in self.stories.values()]

    def put_stories(self, stories: list[Story]) -> None:
        # build_stories() が返すのは「今日の新着 + 未発行の繰り越し」だけ。
        # 一覧へ出した過去の話題まで置換すると、Slack の古いボタンを
        # listed_days の期限内でも押せなくなるため、listed は保持する。
        retained = {story_id: raw for story_id, raw in self.stories.items()
                    if raw.get("status") == "listed"}
        retained.update({s.id: s.to_dict() for s in stories})
        self.stories = retained

    def get_story(self, story_id: str) -> Story | None:
        raw = self.stories.get(story_id)
        return Story.from_dict(raw) if raw else None

    def prune_listed(self, *, today: str, days: int = 90) -> int:
        """`listed` のまま残った話題を刈る。

        Slackはフリープランで履歴90日。それを過ぎるとボタンが押せなくなるので、
        記事化の候補として持っている意味がなくなる。**記事そのものはVaultの
        記事ノートに残る**ので、情報は失われない。
        """
        from datetime import date, timedelta
        try:
            today_d = date.fromisoformat(today)
        except ValueError:
            return 0
        removed = 0
        for story_id, story in list(self.stories.items()):
            if story.get("status") != "listed":
                continue
            try:
                when = date.fromisoformat(str(story.get("listed_on") or "")[:10])
            except ValueError:
                continue
            if (today_d - when) > timedelta(days=days):
                self.stories.pop(story_id, None)
                self.story_urls.pop(story_id, None)
                self.story_articles.pop(story_id, None)
                self.story_notes.pop(story_id, None)
                removed += 1
        for listed_on in list(self.listed_messages):
            try:
                when = date.fromisoformat(str(listed_on)[:10])
            except ValueError:
                continue
            if (today_d - when) > timedelta(days=days):
                self.listed_messages.pop(listed_on, None)
        return removed

    def mark_listed(self, story_ids: list[str], date_str: str) -> None:
        for story_id in story_ids:
            story = self.stories.get(story_id)
            if story is not None:
                story["status"] = "listed"
                story["listed_on"] = date_str

    def mark_written(self, story_id: str, *, date_str: str,
                     thread_ts: str, permalink: str = "") -> None:
        """記事化済みとして記録する。再チェック時の参照に使う（AC-27）。

        **`stories` からは消さない。**消すと記事化が朝のファイルへ書くことに
        なり、同時に動いたとき衝突する。`is_written` は `written` だけを見る
        ので、除外の判定はこれで足りる。`stories` 側は90日で刈られる。
        """
        story = self.stories.get(story_id)
        self.written[story_id] = {
            "date": date_str, "thread_ts": thread_ts,
            "permalink": permalink,
            "title": (story or {}).get("title", ""),
            "category": (story or {}).get("category", "ai"),
            "listed_on": (story or {}).get("listed_on", date_str),
        }

    def is_written(self, story_id: str) -> bool:
        return story_id in self.written

    # -- 入出力 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_success_at": self.last_success_at,
            "source_success_at": self.source_success_at,
            "seen": self.seen,
            "title_norm": self.title_norm,
            "stories": self.stories,
            "written": self.written,
            "pending": self.pending,
            "story_urls": self.story_urls,
            "story_articles": self.story_articles,
            "story_notes": self.story_notes,
            "listed_messages": self.listed_messages,
            "weekly_posted": self.weekly_posted,
            "weekly_unsaved": self.weekly_unsaved,
            "written_articles": self.written_articles,
            "redo": self.redo,
            "usage": self.usage,
            "usage_warned": self.usage_warned,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "State":
        return cls(
            last_success_at=d.get("last_success_at"),
            source_success_at=dict(d.get("source_success_at") or {}),
            seen=dict(d.get("seen") or {}),
            title_norm=dict(d.get("title_norm") or {}),
            stories=dict(d.get("stories") or {}),
            written=dict(d.get("written") or {}),
            pending=list(d.get("pending") or []),
            story_urls={k: list(v) for k, v in (d.get("story_urls") or {}).items()},
            story_articles={k: list(v) for k, v
                            in (d.get("story_articles") or {}).items()},
            story_notes={k: list(v) for k, v
                         in (d.get("story_notes") or {}).items()},
            listed_messages={k: list(v) for k, v
                             in (d.get("listed_messages") or {}).items()},
            weekly_posted=dict(d.get("weekly_posted") or {}),
            weekly_unsaved=dict(d.get("weekly_unsaved") or {}),
            written_articles={k: list(v) for k, v
                              in (d.get("written_articles") or {}).items()},
            redo=[dict(item) for item in (d.get("redo") or []) if isinstance(item, dict)],
            usage={str(k): float(v) for k, v in (d.get("usage") or {}).items()},
            usage_warned=dict(d.get("usage_warned") or {}),
        )


#: ファイルごとに持つキー。書き手が別なので、同時に動いても潰し合わない。
SECTIONS: dict[str, tuple[str, ...]] = {
    "collect": ("last_success_at", "source_success_at", "seen", "title_norm"),
    "stories": ("stories", "story_urls", "story_articles", "story_notes"),
    "written": ("written", "written_articles"),
    # listed_messages は朝（投稿）と記事化（更新で通数が増えたとき）の両方が
    # 書く。両方が保存する outbox に置く。
    "outbox": ("pending", "listed_messages", "weekly_posted", "weekly_unsaved", "redo",
               "usage", "usage_warned"),
}

STATE_DIR = "state"
LEGACY_FILE = "state.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        # 壊れていても止めない。その区画だけ空から始める。
        return {}
    return data if isinstance(data, dict) else {}


def load(root: Path) -> State:
    """`root/state/` から読む。無ければ旧来の `root/state.json` から読む。"""
    merged: dict[str, Any] = {}

    legacy = root / LEGACY_FILE
    if legacy.exists():
        merged.update(_read_json(legacy))

    for name in SECTIONS:
        merged.update(_read_json(root / STATE_DIR / f"{name}.json"))

    try:
        return State.from_dict(merged)
    except (AttributeError, TypeError, ValueError):
        return State()


def save(state: State, root: Path, *, sections: tuple[str, ...] | None = None
         ) -> list[Path]:
    """自分が持ち場としている区画だけを書く。

    `sections` を省略すると全部書く（シミュレーションと移行用）。
    書き込みは一時ファイル経由の置換にして、途中で落ちても壊れないようにする。
    """
    names = sections if sections is not None else tuple(SECTIONS)
    data = state.to_dict()
    directory = root / STATE_DIR
    directory.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name in names:
        keys = SECTIONS[name]
        path = directory / f"{name}.json"
        payload = {key: data[key] for key in keys}
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        temporary.replace(path)
        written.append(path)

    # 旧来の1ファイルが残っていたら、移行が済んだ時点で消す。
    legacy = root / LEGACY_FILE
    if legacy.exists() and sections is None:
        legacy.unlink()
    return written
