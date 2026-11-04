"""保存できなかった日の知識を、紙面の PDF から積み直す（一度きりの復旧用）。

    python scripts/restore_knowledge.py <PDFのフォルダ> <YYYY-MM-DD> [--apply]

--apply が無いときは件数だけを出す。--apply で state の持ち越しへ入れ、次の朝の処理が
Vault へ積む。紙面に主題は載っていないので、主題は「未分類」になる。
ログは公開なので、紙面の中身は出さない。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import knowledge  # noqa: E402
from src.state import load, save  # noqa: E402

ARTICLE_HEAD = re.compile(r"^\d+/\d+\n+朝刊\n", re.M)
URL = re.compile(r"https?://[^\s／]+")


def pdf_text(path: Path) -> str:
    return subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def join_lines(lines: list[str]) -> str:
    """折り返しを戻す。英数字どうしの間だけ空白を残す。純粋関数。"""
    out = ""
    for line in (x.strip() for x in lines):
        ascii_gap = out[-1:].isascii() and out[-1:].isalnum() and line[:1].isascii()
        out += (" " if ascii_gap else "") + line
    return out


def split_points(text: str) -> list[str]:
    """空白でつながった要点を分ける。日本語の後の空白だけを区切りと見る。純粋関数。"""
    return [p.strip() for p in re.split(r"(?<=[^\x00-\x7f]) +", text) if p.strip()]


def parse_sheet(text: str) -> dict | None:
    """紙面1本の文字から見出し・要点・出典を取る。取れなければ None。純粋関数。"""
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        return None
    lead = join_lines(blocks[0].split("\n"))
    label = next((i for i, b in enumerate(blocks) if b.strip() == "要点"), None)
    points: list[str] = []
    if label is not None and label + 1 < len(blocks):
        points = split_points(join_lines(blocks[label + 1].split("\n")))
    at = text.rfind("出典")
    # URL の途中の折り返しを戻してから拾う
    sources = URL.findall(re.sub(r"\n(?=[^\s／]*[/\-_.\w])", "", text[at:])) if at >= 0 else []
    if not sources or not (points or lead):
        return None
    return {"lead": lead, "facts": points or [lead], "sources": sources}


def parse_paper(text: str) -> list[dict]:
    """紙面 PDF 1つの文字を記事ごとに分ける。純粋関数。"""
    parts = ARTICLE_HEAD.split(text)[1:]
    return [s for s in (parse_sheet(p) for p in parts) if s]


def to_entries(sheets: list[dict], state, date: str) -> list[knowledge.Entry]:
    """出典の URL で話題を突き止めて知識の項目にする。純粋関数。"""
    by_url = {url.rstrip("/"): sid for sid, urls in state.story_urls.items() for url in urls}
    entries: dict[str, knowledge.Entry] = {}
    for sheet in sheets:
        sid = next((by_url[u.rstrip("/")] for u in sheet["sources"]
                    if u.rstrip("/") in by_url), None)
        if sid is None:
            continue
        story = state.stories.get(sid) or {}
        entries[sid] = knowledge.Entry(
            story_id=sid, date=date, category=str(story.get("category") or "ai"),
            topic=knowledge.UNSORTED, headline=sheet["lead"], facts=sheet["facts"],
            sources=sheet["sources"], notes=list(state.story_notes.get(sid, [])))
    return list(entries.values())


def main(argv: list[str]) -> int:
    folder, date = Path(argv[0]), argv[1]
    root = Path(__file__).resolve().parent.parent
    state = load(root)
    sheets = [s for f in sorted(folder.glob("*.pdf")) for s in parse_paper(pdf_text(f))]
    entries = to_entries(sheets, state, date)
    facts = sum(len(e.facts) for e in entries)
    print(f"紙面の記事 {len(sheets)}件 / 話題を特定 {len(entries)}件 / 要点 {facts}個")
    if "--apply" in argv:
        knowledge.carry(state, knowledge.carried(state) + entries)
        save(state, root, sections=("outbox",))
        print(f"持ち越しへ入れました: {len(state.knowledge_pending)}件")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
