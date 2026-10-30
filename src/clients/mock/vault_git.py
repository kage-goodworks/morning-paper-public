"""Vault 書き込みのモック。

push せず `out/vault/` 以下へ実ファイルを書く。**Markdown の生成そのものは
モックしない**（`src/note.py` が本番と同じものを作る）。ここは書き先を
差し替えるだけ。

`read` を持つのは、記事化が「保存済みのノートを読み直す」流れだからで、
モックでも同じ経路を通す。
"""

from __future__ import annotations

from .context import MockContext


class MockNoteStore:
    """`NoteStore` のモック。"""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx
        self.root = ctx.out / "vault"
        self.commits: list[str] = []

    def write(self, rel_path: str, content: str) -> None:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read(self, rel_path: str) -> str | None:
        path = self.root / rel_path
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def commit_push(self, message: str) -> None:
        # 自動化のcommitは先頭へ auto: を付ける（AI_GUIDE 自動化節）
        if not message.startswith("auto:"):
            message = "auto: " + message
        self.commits.append(message)
        log = self.ctx.out / "vault_commits.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(message + "\n")
