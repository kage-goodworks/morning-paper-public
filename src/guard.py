"""秘密の検出と伏せ字化。

**検出したパターン名と位置だけを報告し、値そのものは出さない。**伏せ字にした
うえで push は続行する。黙って伏せ字にすると、本当に鍵が漏れかけていた事実に
気づけなくなるため、通知は必須（NFR-2 / AC-22）。

外部I/Oを持たない純粋関数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: トークンの途中で拾わないための先頭境界。`\b` は使わない ―― 日本語の直後
#: （「キーはsk-...です」）では境界が成立せず、取りこぼすため。
_HEAD = r"(?<![A-Za-z0-9_-])"

#: 既知のパターン。名前だけが通知に載る。
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(_HEAD + r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_key", re.compile(_HEAD + r"sk-[A-Za-z0-9_-]{16,}")),
    ("slack_bot_token", re.compile(_HEAD + r"xox[bpars]-[A-Za-z0-9-]{10,}")),
    ("github_token", re.compile(_HEAD + r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("google_api_key", re.compile(_HEAD + r"AIza[A-Za-z0-9_-]{20,}")),
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("xai_key", re.compile(_HEAD + r"xai-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(_HEAD + r"AKIA[0-9A-Z]{16}")),
]

REDACTION = "[REDACTED: {name}]"


@dataclass
class Finding:
    """検出結果。**値そのものは保持しない。**"""
    pattern: str
    line: int
    column: int
    section: str = ""


@dataclass
class GuardResult:
    text: str
    findings: list[Finding]

    @property
    def clean(self) -> bool:
        return not self.findings


def _section_at(lines: list[str], index: int) -> str:
    """その行が属する直近の見出しを返す。通知の位置情報に使う。"""
    for i in range(index, -1, -1):
        stripped = lines[i].lstrip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def scan(text: str, *, env_values: list[str] | None = None) -> GuardResult:
    """検出して伏せ字へ置換する。処理は止めない。

    `env_values` には環境変数に載っている値そのものを渡せる。パターンに
    当てはまらない秘密も落とすため。**渡された値はログにも戻り値にも出ない。**
    """
    lines = (text or "").split("\n")
    findings: list[Finding] = []

    extra: list[tuple[str, re.Pattern[str]]] = []
    for value in env_values or []:
        if value and len(value) >= 8:
            extra.append(("env_value", re.compile(re.escape(value))))

    for index, line in enumerate(lines):
        for name, pattern in PATTERNS + extra:
            while True:
                match = pattern.search(line)
                if not match:
                    break
                findings.append(Finding(pattern=name, line=index + 1,
                                        column=match.start() + 1,
                                        section=_section_at(lines, index)))
                line = line[:match.start()] + REDACTION.format(name=name) + \
                    line[match.end():]
        lines[index] = line

    return GuardResult(text="\n".join(lines), findings=findings)


def build_report(path: str, findings: list[Finding]) -> str:
    """伏せ字にした位置の通知文。値は載せない（AC-22）。"""
    if not findings:
        return ""
    lines = [
        ":lock: 秘密らしき文字列を伏せ字にしました",
        "",
        f"ファイル: {path}",
        f"件数: {len(findings)}",
        "",
        "検出箇所:",
    ]
    for f in findings:
        where = f"{f.line}行{f.column}文字目"
        if f.section:
            where += f"（{f.section}）"
        lines.append(f"・{where} — パターン: {f.pattern}")
    lines.extend([
        "",
        "値そのものは表示していません。該当のキーは漏洩した可能性があるため、",
        "ローテーションを検討してください。push は続行済みです。",
    ])
    return "\n".join(lines)


def status_line(findings: list[Finding]) -> str:
    """`取得状況` へ載せる1行。"""
    if not findings:
        return ""
    sections = sorted({f.section for f in findings if f.section})
    where = "、".join(f"`{s}`セクション" for s in sections) or "本文"
    names = sorted({f.pattern for f in findings})
    return f"伏せ字: {len(findings)}件（{where}、{'/'.join(names)}）"
