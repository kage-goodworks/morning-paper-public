"""LLM のモック（Claude / Gemini）。

LLMを使うのは翻訳・執筆・YouTube要約の3か所だけなので、`purpose` で分ける。

**降格（claude → gemini）はモックしない。**どちらのモデルを呼ぶかの判断は
本番と同じ `src/write.py` が行い、ここは呼ばれたら返すだけ。`--fail claude`
を付けると Claude 側だけが失敗し、降格の経路が実際に踏まれる。
"""

from __future__ import annotations

import re

from .context import MockContext

TITLE_RE = re.compile(r"^(?:\d+\.\s*)?TITLE:\s*(.+)$", re.M)   # 「n. TITLE:」も拾う
COUNT_RE = re.compile(r"^COUNT:\s*(\d+)$", re.M)


class _MockLLM:
    name = "mock"

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def complete(self, prompt: str, purpose: str) -> str:
        if purpose == "translate":
            return self._translate(prompt)
        if purpose == "gallery":
            titles = TITLE_RE.findall(prompt)
            return "\n".join(f"{i}. {t.strip()[:20]}の配列とキーキャップが特徴"
                             for i, t in enumerate(titles, start=1))
        if purpose == "youtube":
            return ("動画の要点を3点にまとめた要約。取り上げられた発表と、"
                    "評価が分かれている点、今後の見通しに触れている。")
        if purpose == "write":
            return self._write(prompt)
        if purpose == "weekly":
            return self._weekly(prompt)
        return ""

    def _translate(self, prompt: str) -> str:
        """見出しの翻訳と短縮。Spec のモック定義どおり先頭へ `[翻訳]` を付ける。

        本番と同じ「番号. 訳文 || 短縮見出し」の形で返す。短縮見出しは題名を
        そのまま返す（モックでは縮めない。一覧の題名が変わると検証しにくいため）。
        """
        titles = TITLE_RE.findall(prompt)
        return "\n".join(f"{i}. [翻訳]{t.strip()} || {t.strip()}"
                         for i, t in enumerate(titles, start=1))

    def _write(self, prompt: str) -> str:
        """定型記事を `<!-- ARTICLE n -->` 区切りで返す。"""
        titles = TITLE_RE.findall(prompt)
        count = len(titles) or int((COUNT_RE.search(prompt) or [0, "1"])[1])
        out: list[str] = []
        for i in range(count):
            n = (i % 5) + 1
            template = self.ctx.read_text("articles", f"article{n}.md") or "## {title}\n"
            title = titles[i] if i < len(titles) else f"話題{i + 1}"
            body = template.replace("{title}", title).replace("{date}", "本日")
            # 知識の部品。主題は見出しの最初の語（同じ会社の話題は同じ主題にまとまる）
            topic = (title.split() or ["話題"])[0]
            body = (body.rstrip("\n") + f"\nTOPIC: {topic}\nKNOW:\n"
                    f"- {title}が本日発表された\n- 提供地域が3から12に増えた\n")
            out.append(f"<!-- ARTICLE {i + 1} -->\n{body}")
        return "\n".join(out)

    def _weekly(self, prompt: str) -> str:
        titles = TITLE_RE.findall(prompt)
        if not titles:
            return ""
        parts = []
        for i, t in enumerate(titles, 1):
            parts.append(
                f"<!-- ARTICLE {i} -->\n## {t}\n\n"
                "収集当時は性能面への評価が中心だったが、その後は実際に運用した\n"
                "報告が出はじめ、論点が導入コストへ移っている。反応量そのものは\n"
                "収集時点より増えている。\n")
        return "\n".join(parts)


class MockClaude(_MockLLM):
    name = "claude"


class MockGemini(_MockLLM):
    name = "gemini"
