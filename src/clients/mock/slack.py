"""Slack のモック。

投稿せず `out/slack/<連番>-<種別>.json` へペイロードを書く。
`out/slack/*.json` は Block Kit Builder へ貼れば見た目を確認できる。
"""

from __future__ import annotations

import json

from .context import MockContext


class MockSlackClient:
    """`SlackClient` のモック。"""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx
        self.dir = ctx.out / "slack"

    def _dump(self, kind: str, payload: dict) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        seq = self.ctx.next_seq()
        path = self.dir / f"{seq:03d}-{kind}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        ts = f"{1756000000 + seq}.000100"
        permalink = "https://slack.invalid/archives/C_MOCK/p" + ts.replace(".", "")
        if kind in ("post", "files"):
            # 「チャンネルの履歴」の代わり。find_message が読む
            with (self.dir / "_history.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"text": payload.get("text", ""), "ts": ts,
                                    "permalink": permalink}, ensure_ascii=False) + "\n")
        return {"ok": True, "ts": ts, "channel": "C_MOCK",
                "permalink": permalink, "_path": str(path)}

    def find_message(self, text: str, *, within_hours: float = 36.0,
                     channel: str | None = None) -> dict | None:
        path = self.dir / "_history.jsonl"
        if not path.exists():
            return None
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            entry = json.loads(line)
            if entry.get("text") == text:
                return {"ts": entry["ts"], "permalink": entry["permalink"]}
        return None

    def post_message(self, blocks: list[dict], text: str) -> dict:
        return self._dump("post", {"text": text, "blocks": blocks})

    def reply_in_thread(self, thread_ts: str, blocks: list[dict], text: str) -> dict:
        return self._dump("thread", {"thread_ts": thread_ts, "text": text,
                                     "blocks": blocks})

    def upload_image(self, thread_ts: str, path: str, title: str) -> dict:
        return self._dump("image", {"thread_ts": thread_ts, "file": path,
                                    "title": title})

    def upload_files(self, paths: list[str], text: str, thread_ts: str = "",
                     channel: str | None = None) -> dict:
        return self._dump("files", {"thread_ts": thread_ts, "files": list(paths),
                                    "text": text, "channel": channel or ""})

    def update_message(self, ts: str, blocks: list[dict], text: str) -> dict:
        return self._dump("update", {"ts": ts, "text": text, "blocks": blocks})

    def notify_failure(self, report: str) -> dict:
        return self._dump("failure", {"text": report})
