"""state の三方向マージ。Actions の「状態のcommit」で使う。

    python scripts/merge_state.py <BASE_DIR> <OURS_DIR> <TARGET_DIR>

- BASE   … 実行の最初にチェックアウトした時点の state
- OURS   … この実行が書いた state
- TARGET … `origin/main` の最新（他の実行が先に push したもの）。ここへ結果を書く

朝・記事化・週次は concurrency で直列にしているが、**前の実行が push した直後に
次の実行がチェックアウトする**と、次の実行の土台は古いままになる。
`written.json` のような「辞書の末尾に項目を足す」ファイルは git の rebase では
必ず競合し、3回とも失敗して記録が消えた（2026-10-01、9/30 の 73 件）。
JSON として意味で併合すれば競合は起きない。

規則（純粋関数 `merge`）:

- 辞書: 両方にある鍵は再帰。片方だけが足した鍵は残す。片方が**消した**鍵
  （BASE にあって、その側に無い）は消えたままにする
- リスト: 集合として扱う。片方が消した要素は消え、片方が足した要素は足す。
  並びは TARGET のものを先に、OURS が足した分を後ろに
- それ以外（文字列・数値）: OURS が BASE から変えていれば OURS、変えていなければ TARGET
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

MISSING = object()


def merge(base: Any, ours: Any, theirs: Any) -> Any:
    """三方向マージ。純粋関数。"""
    if isinstance(ours, dict) and isinstance(theirs, dict):
        base_d = base if isinstance(base, dict) else {}
        out: dict = {}
        keys = list(theirs) + [k for k in ours if k not in theirs]
        for key in keys:
            in_ours, in_theirs, in_base = key in ours, key in theirs, key in base_d
            if in_ours and in_theirs:
                out[key] = merge(base_d.get(key, MISSING), ours[key], theirs[key])
            elif in_ours:
                if in_base:
                    continue            # 相手が消した
                out[key] = ours[key]    # 自分が足した
            else:
                if in_base and base_d[key] == theirs[key]:
                    continue            # 自分が消した（相手は触っていない）
                out[key] = theirs[key]  # 相手が足した／変えた
        return out

    if isinstance(ours, list) and isinstance(theirs, list):
        base_l = base if isinstance(base, list) else []
        removed_by_ours = [x for x in base_l if x not in ours]
        removed_by_theirs = [x for x in base_l if x not in theirs]
        out_l = [x for x in theirs if x not in removed_by_ours]
        out_l += [x for x in ours if x not in out_l and x not in removed_by_theirs]
        return out_l

    if base is MISSING:
        return ours
    return ours if ours != base else theirs


def _load(path: Path) -> Any:
    if not path.exists():
        return MISSING
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return MISSING


def merge_dirs(base_dir: Path, ours_dir: Path, target_dir: Path) -> list[Path]:
    """OURS にあるファイルを TARGET へ併合して書く。書いたファイルを返す。"""
    written: list[Path] = []
    for ours_path in sorted(p for p in ours_dir.rglob("*") if p.is_file()):
        rel = ours_path.relative_to(ours_dir)
        target = target_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if ours_path.suffix != ".json":
            shutil.copyfile(ours_path, target)      # YAML などは自分のものをそのまま
            written.append(target)
            continue
        ours = _load(ours_path)
        theirs = _load(target)
        base = _load(base_dir / rel)
        if ours is MISSING:
            continue
        merged = ours if theirs is MISSING else merge(base, ours, theirs)
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        written.append(target)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    base_dir, ours_dir, target_dir = (Path(a) for a in argv[1:])
    for path in merge_dirs(base_dir, ours_dir, target_dir):
        print(f"併合: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
