"""失敗の総当たり。

外部を呼ぶ箇所を1つずつ、3通りの壊し方で壊し、そのあと健全な状態で再実行して
「約束」が守られるかを機械的に確かめる。指摘された1件ずつを直すのではなく、
同じ種類の穴を一度に洗い出すための試験。

壊し方:
- always: 3回とも失敗（相手が落ちている）
- lost:   相手は処理したのに応答が届かない（タイムアウト。副作用だけ残る）
- crash:  その直後にプロセスが死ぬ（state は保存されない。副作用だけ残る）

約束（再実行のあと）:
- 朝: 同じ日の一覧はSlackに1通だけ。一覧に出した記事はいつかVaultに残る
      （その日に残せなければ翌日拾い直す）。保留は残らない
- 記事化: 選んだ話題はすべて作成済みになる。作成済みは二度作らない
- 週次: 同じ週の投稿は1通だけ。週次ノートはVaultに残る

「lost」「crash」で副作用が二重になるものは、防げない理由と気づき方を
`KNOWN_RESIDUAL` に明記する。ここに無い二重は不具合。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src import articles as articles_mod
from src import main as main_mod
from src import render
from src import weekly as weekly_mod
from src.clients.factory import build_mock
from src.config import load as load_config
from src.pipeline import Run
from src.retry import NO_SLEEP
from src.state import State
from src.state import load as load_state, save as save_state

ALL_SECTIONS = ("collect", "stories", "written", "outbox")


class Crash(BaseException):
    """プロセスが死ぬ。`guarded` の `except Exception` に捕まらない。"""


def _make(tmp_path: Path, *, day: int, date_str: str, phase: str = "morning",
          state: State | None = None) -> Run:
    cfg = load_config()
    clients = build_mock(scenario="normal", day=day, out_dir=tmp_path, phase=phase)
    run = Run(config=cfg, clients=clients, date_str=date_str, out_dir=tmp_path,
              data_dir=tmp_path, sleep=NO_SLEEP)
    if state is not None:
        # 本番と同じく、外への書き込みの直後に state をディスクへ書く
        run.checkpoint = lambda: save_state(state, tmp_path, sections=ALL_SECTIONS)
    return run


def _after_crash(tmp_path: Path, before: State) -> State:
    """プロセスが死んだ後の state。途中保存があればそれ、無ければ前の state。"""
    if (tmp_path / "state").exists():
        return load_state(tmp_path)
    return before


def _inject(target, name: str, mode: str, monkeypatch) -> None:
    """`target.name` を壊す。"""
    original = getattr(target, name)
    state = {"done": False}

    def broken(*args, **kwargs):
        if mode == "always":
            raise RuntimeError("HTTP 503 Service Unavailable")
        if state["done"]:
            return original(*args, **kwargs)
        state["done"] = True
        result = original(*args, **kwargs)        # 副作用は起きる
        if mode == "lost":
            raise TimeoutError("timeout: 応答が届かない")
        raise Crash()

    monkeypatch.setattr(target, name, broken)


def _slot(run: Run, spec: str):
    """'slack.post_message' のような指定を (対象, メソッド名) に。"""
    if spec.startswith("render."):
        return render, spec.split(".", 1)[1]
    head, name = spec.rsplit(".", 1)
    obj = run.clients
    for part in head.split("."):
        if "[" in part:
            attr, key = part[:-1].split("[")
            container = getattr(obj, attr)
            key = key.strip("'\"")
            obj = container[int(key)] if isinstance(container, list) else container[key]
        else:
            obj = getattr(obj, part)
    return obj, name


class SlackLog:
    """モックの Slack 出力を実行ごとに退避する。

    モックの連番は実行ごとに1から始まるので、そのままだと次の実行の
    ファイルが前の実行のものを上書きし、投稿の数を数えられない。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.items: list[tuple[str, dict]] = []

    def collect(self) -> None:
        d = self.root / "slack"
        if not d.exists():
            return
        for f in sorted(d.glob("*.json")):
            kind = f.stem.split("-", 1)[1]
            self.items.append((kind, json.loads(f.read_text(encoding="utf-8"))))
            f.unlink()

    def list_posts(self, date_str: str) -> list[dict]:
        return [d for kind, d in self.items
                if kind == "post" and d.get("text") == f"{date_str} のニュース"]

    def completions(self) -> list[dict]:
        return [d for kind, d in self.items if kind == "files"]

    def posts_containing(self, text: str) -> list[dict]:
        return [d for kind, d in self.items if kind in ("post", "files")
                and text in json.dumps(d, ensure_ascii=False)]


def _note_ids(root: Path) -> set[str]:
    ids = set()
    for f in (root / "vault" / "40_Knowledge" / "News").rglob("*.md"):
        ids.add(f.stem.rsplit("-", 1)[-1])
    return ids


# --------------------------------------------------------------------------
# 朝の処理
# --------------------------------------------------------------------------

MORNING_POINTS = [
    "feed.fetch_articles", "releases.fetch", "quotes.fetch",
    "extractors[1].extract", "llms[gemini].complete", "reactions.fetch",
    "notes.read", "notes.write", "notes.commit_push",
    "slack.post_message", "slack.notify_failure",
]
MODES = ["always", "lost", "crash"]

#: 防げない二重。理由と気づき方を書く
KNOWN_RESIDUAL: dict[tuple[str, str], str] = {}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("point", MORNING_POINTS)
def test_朝の処理_どこで壊れても再実行で約束が守られる(tmp_path, monkeypatch, point, mode):
    state = State()
    run = _make(tmp_path, day=1, date_str="2026-09-01", state=state)
    before = copy.deepcopy(state)
    target, name = _slot(run, point)
    _inject(target, name, mode, monkeypatch)

    log = SlackLog(tmp_path)
    try:
        main_mod.run_morning(run, state)
    except Crash:
        state = _after_crash(tmp_path, before)
    monkeypatch.undo()
    log.collect()

    # 同じ日の保険の起動（健全）
    run2 = _make(tmp_path, day=1, date_str="2026-09-01", state=state)
    main_mod.run_morning(run2, state)
    log.collect()
    # 翌日（健全）。本番のフィードは過去の記事も載せ続けるので、前日の
    # フィード（day=1）をもう一度読む形で近づける
    run3 = _make(tmp_path, day=1, date_str="2026-09-02", state=state)
    main_mod.run_morning(run3, state)
    log.collect()

    posts = log.list_posts("2026-09-01")
    if (point, mode) in KNOWN_RESIDUAL:
        assert len(posts) >= 1
    else:
        assert len(posts) == 1, f"{point}/{mode}: 同じ日の一覧が{len(posts)}通"

    # 一覧に出した記事は、その日か翌日のうちに Vault に残る
    listed_day1 = [s for s in state.all_stories()
                   if s.listed_on == "2026-09-01" and s.kind != "gallery"]
    notes = _note_ids(tmp_path)
    missing = [a for s in listed_day1 for a in s.article_ids if a not in notes]
    assert not missing, f"{point}/{mode}: ノートが無い記事 {missing[:3]}"

    assert state.pending == [], f"{point}/{mode}: 保留が残っている"
    assert state.last_success_at, f"{point}/{mode}: 前回成功時点が無い"
    assert state.listed_messages.get("2026-09-01"), f"{point}/{mode}: 一覧の記録が無い"
    assert state.listed_messages.get("2026-09-02"), f"{point}/{mode}: 翌日の一覧が出ていない"


# --------------------------------------------------------------------------
# 記事化
# --------------------------------------------------------------------------

ARTICLE_POINTS = [
    "notes.read", "llms[claude].complete", "llms[gemini].complete",
    "render.capture_article_pdf", "slack.upload_files", "slack.update_message",
    "slack.post_message",
]

KNOWN_RESIDUAL_ARTICLES: dict[tuple[str, str], str] = {}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("point", ARTICLE_POINTS)
def test_記事化_どこで壊れても再実行で約束が守られる(tmp_path, monkeypatch, point, mode):
    state = State()
    morning = _make(tmp_path, day=1, date_str="2026-09-01")
    result = main_mod.run_morning(morning, state)
    selected = [s.id for s in state.all_stories() if s.status == "listed"][:7]
    log = SlackLog(tmp_path)
    log.collect()
    before = copy.deepcopy(state)

    save_state(state, tmp_path, sections=ALL_SECTIONS)      # 朝の終わりの保存
    run = _make(tmp_path, day=1, date_str="2026-09-01", phase="click", state=state)
    target, name = _slot(run, point)
    _inject(target, name, mode, monkeypatch)
    try:
        articles_mod.run_articles(run, state, story_ids=selected,
                                  message_ts=result["message_ts"])
    except Crash:
        state = _after_crash(tmp_path, before)
    monkeypatch.undo()
    log.collect()

    # もう一度押す（健全）
    run2 = _make(tmp_path, day=1, date_str="2026-09-01", phase="click", state=state)
    out = articles_mod.run_articles(run2, state, story_ids=selected,
                                    message_ts=result["message_ts"])
    log.collect()

    assert all(state.is_written(s) for s in selected), f"{point}/{mode}: 作成済みでない話題がある"
    assert out["pending"] == [], f"{point}/{mode}: 保留が残っている"
    completions = [c for c in log.completions() if c["text"].startswith("✅")]
    total_written = sum(int(c["text"].split("件")[0].split()[-1]) for c in completions)
    if (point, mode) in KNOWN_RESIDUAL_ARTICLES:
        assert total_written >= len(selected)
    else:
        assert total_written == len(selected), (
            f"{point}/{mode}: 完了通知の合計が {total_written} 件（選んだのは {len(selected)} 件）")


# --------------------------------------------------------------------------
# 週次レビュー
# --------------------------------------------------------------------------

WEEKLY_POINTS = [
    "notes.read", "reactions.fetch", "llms[claude].complete",
    "render.capture_pdf", "slack.upload_files", "notes.write", "notes.commit_push",
]

KNOWN_RESIDUAL_WEEKLY: dict[tuple[str, str], str] = {}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("point", WEEKLY_POINTS)
def test_週次_どこで壊れても再実行で約束が守られる(tmp_path, monkeypatch, point, mode):
    state = State()
    log = SlackLog(tmp_path)
    for day, date_str in ((1, "2026-09-01"), (2, "2026-09-02"), (3, "2026-09-03")):
        main_mod.run_morning(_make(tmp_path, day=day, date_str=date_str), state)
        log.collect()
    before = copy.deepcopy(state)
    save_state(state, tmp_path, sections=ALL_SECTIONS)

    run = _make(tmp_path, day=3, date_str="2026-09-08", phase="weekly", state=state)
    target, name = _slot(run, point)
    _inject(target, name, mode, monkeypatch)
    try:
        weekly_mod.run_weekly(run, state)
    except Crash:
        state = _after_crash(tmp_path, before)
    monkeypatch.undo()
    log.collect()

    run2 = _make(tmp_path, day=3, date_str="2026-09-08", phase="weekly", state=state)
    out = weekly_mod.run_weekly(run2, state)
    log.collect()
    week = out["week"]

    posts = log.posts_containing("反応の変化")
    if (point, mode) in KNOWN_RESIDUAL_WEEKLY:
        assert len(posts) >= 1
    else:
        assert len(posts) == 1, f"{point}/{mode}: 同じ週の投稿が{len(posts)}通"
    assert state.weekly_posted.get(week), f"{point}/{mode}: 投稿済みの記録が無い"
    assert (tmp_path / "vault" / "40_Knowledge" / "News-Review" / f"{week}.md").exists(), \
        f"{point}/{mode}: 週次ノートが Vault に無い"
    assert week not in state.weekly_unsaved, f"{point}/{mode}: 未保存の控えが残っている"
