"""執筆結果の部品分解。純粋関数。

Claude（降格時は Gemini）が返す1記事は、次の見出し付きの部品で構成される。

    LEAD: 一言リード
    RECAP:
    前回までの要点（続報のときだけ）
    POINTS:
    - 要点
    BODY:
    本文
    WHY:
    自分との関係
    TERMS:
    - 用語: 一言解説

LLM は見出しを落としたり順番を変えたりすることがあるので、**どの部品が
欠けても本文だけで紙面が成立する**ように読む。見出しが1つも無ければ
全体を本文として扱う（従来の出力や、降格先のモデルの出力に耐える）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SECTION_NAMES = ("LEAD", "RECAP", "POINTS", "BODY", "WHY", "TERMS", "TOPIC", "KNOW")

_HEADING = re.compile(
    r"^\s*(?:\*\*|#+\s*)?(LEAD|RECAP|POINTS|BODY|WHY|TERMS|TOPIC|KNOW)\s*[:：]\s*(?:\*\*)?\s*(.*)$",
    re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*・●]|\d+[.)．])\s*(.+?)\s*$")


@dataclass
class Sections:
    lead: str = ""
    recap: str = ""
    points: list[str] = field(default_factory=list)
    body: str = ""
    why: str = ""
    terms: list[tuple[str, str]] = field(default_factory=list)
    #: 知識の主題名（紙面には出さない。Vault の知識ノートの振り分けに使う）
    topic: str = ""
    #: 知識として残す事実（紙面には出さない）
    knowledge: list[str] = field(default_factory=list)

    @property
    def is_structured(self) -> bool:
        return bool(self.lead or self.recap or self.points or self.why or self.terms)


def _bullets(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        m = _BULLET.match(line)
        if m:
            out.append(m.group(1))
        elif line.strip() and out:
            out[-1] += " " + line.strip()   # 折り返し行はつなぐ
        elif line.strip():
            out.append(line.strip())
    return out


def _terms(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in _bullets(text):
        # 「用語: 説明」「用語：説明」「用語 — 説明」を受ける
        m = re.match(r"^\**(.+?)\**\s*[:：—―-]\s*(.+)$", item)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
        else:
            out.append((item, ""))
    return out


def parse(text: str) -> Sections:
    """1記事の文字列を部品へ分ける。"""
    chunks: dict[str, list[str]] = {name: [] for name in SECTION_NAMES}
    current: str | None = None
    found = False
    for line in (text or "").splitlines():
        m = _HEADING.match(line)
        if m:
            found = True
            current = m.group(1).upper()
            rest = m.group(2).strip()
            if rest:
                chunks[current].append(rest)
            continue
        if current is None:
            chunks["BODY"].append(line)
        else:
            chunks[current].append(line)

    if not found:
        return Sections(body=(text or "").strip())

    def joined(name: str) -> str:
        return "\n".join(chunks[name]).strip()

    # 「前回まで:」を LLM が書き足すことがある。枠の見出しと重なるので落とす
    recap = re.sub(r"^\s*(前回まで|前回の要点)\s*[:：]\s*", "", joined("RECAP"))

    return Sections(
        lead=" ".join(joined("LEAD").split()),
        recap=recap,
        points=_bullets(joined("POINTS")),
        body=joined("BODY"),
        why=joined("WHY"),          # 段落を保つ（複数段落で書かれるようになった）
        terms=_terms(joined("TERMS")),
        topic=" ".join(joined("TOPIC").split()),
        knowledge=_bullets(joined("KNOW")),
    )
