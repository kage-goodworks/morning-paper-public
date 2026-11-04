"""紙面の文字から知識を積み直す処理（scripts/restore_knowledge.py）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from src.state import State

_spec = importlib.util.spec_from_file_location(
    "restore_knowledge", Path(__file__).parent.parent / "scripts/restore_knowledge.py")
restore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restore)

PAPER = """AI ・ 機 械 学 習

2026-11-04

1/5

朝刊

Claude Code で業務アプリ、日本語要件から 30 分生成

Claude Code バイブコーディング

画像: 元記事より

要点

非エンジニアも業務アプリを自作する時代 Claude for Google Workspaceで裾野拡大 承認フロー付きでメール下書きまで自動生
成

本文の段落。

自分との関係

本人の話。

出典 https://gihyo.jp/article/2026/10/claude-business-guide-01 ／ https://gihyo.jp/article/2

業界動向

2026-11-04

2/5

朝刊

次の見出し

要点

一つ目 二つ目

出典 https://example.com/x
"""


def test_紙面の文字から見出しと要点と出典を取る():
    sheets = restore.parse_paper(PAPER)
    assert len(sheets) == 2
    first = sheets[0]
    assert first["lead"].startswith("Claude Code で業務アプリ")
    assert first["facts"] == ["非エンジニアも業務アプリを自作する時代",
                              "Claude for Google Workspaceで裾野拡大",
                              "承認フロー付きでメール下書きまで自動生成"]
    assert first["sources"][0].endswith("claude-business-guide-01")
    assert "本人の話" not in str(first)


def test_出典のURLで話題を突き止め_分からないものは積まない():
    state = State()
    state.story_urls = {"s1": ["https://example.com/x/"]}
    state.stories = {"s1": {"category": "business"}}
    state.story_notes = {"s1": ["50_Logs/News/a.md"]}
    entries = restore.to_entries(restore.parse_paper(PAPER), state, "2026-11-04")
    assert [e.story_id for e in entries] == ["s1"]
    assert entries[0].category == "business" and entries[0].facts == ["一つ目", "二つ目"]
    assert entries[0].topic == "未分類" and entries[0].notes == ["50_Logs/News/a.md"]
