"""保存できなかった日の知識を、紙面の PDF から積み直す（一度きりの復旧用）。

    python scripts/restore_knowledge.py <PDFのフォルダ> <YYYY-MM-DD> [--apply]

--apply が無いときは、紙面の文字の並びと抜き出せた件数だけを出す。ログは公開なので、
「自分との関係」は出さない。--apply で state の持ち越しへ入れ、次の朝の処理が Vault へ積む。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PRIVATE_LABEL = "自分との関係"
SOURCE_LABEL = "出典"


def pdf_text(path: Path) -> str:
    return subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def without_private(text: str) -> str:
    """「自分との関係」から「出典」の手前までを落とす。純粋関数。"""
    return re.sub(rf"{PRIVATE_LABEL}.*?(?={SOURCE_LABEL}|\Z)", "", text, flags=re.S)


def main(argv: list[str]) -> int:
    folder, date = Path(argv[0]), argv[1]
    files = sorted(folder.glob("*.pdf"))
    print(f"紙面 {len(files)}件 / 対象日 {date}")
    if files:
        print(without_private(pdf_text(files[0]))[:1800])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
