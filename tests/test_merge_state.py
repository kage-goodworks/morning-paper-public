"""scripts/merge_state.py の単体テスト。

Actions の「状態のcommit」で、前の実行が push した state と自分の state を
JSON として併合する。rebase の競合で記録が消えた件（2026-10-01）の再発防止。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_state.py"
spec = importlib.util.spec_from_file_location("merge_state", SCRIPT)
merge_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_state)
merge = merge_state.merge


def test_辞書は両方が足した鍵を残す():
    base = {"written": {"a": 1}}
    ours = {"written": {"a": 1, "b": 2}}          # 自分が b を足した
    theirs = {"written": {"a": 1, "c": 3}}        # 相手が c を足した
    assert merge(base, ours, theirs) == {"written": {"a": 1, "c": 3, "b": 2}}


def test_辞書は片方が消した鍵を消えたままにする():
    base = {"pending": {"x": 1, "y": 2}}
    ours = {"pending": {"y": 2}}                  # 自分が x を消した
    theirs = {"pending": {"x": 1, "y": 2, "z": 3}}
    assert merge(base, ours, theirs) == {"pending": {"y": 2, "z": 3}}
    # 相手が消した鍵も消えたまま
    assert merge(base, theirs, ours) == {"pending": {"y": 2, "z": 3}}


def test_同じ鍵を両方が変えたら自分が勝つ():
    base = {"n": 1}
    assert merge(base, {"n": 2}, {"n": 3}) == {"n": 2}
    # 自分が変えていなければ相手
    assert merge(base, {"n": 1}, {"n": 3}) == {"n": 3}


def test_リストは集合として足し引きする():
    base = ["a", "b"]
    ours = ["b", "c"]            # a を消し c を足した
    theirs = ["a", "b", "d"]     # d を足した
    assert merge(base, ours, theirs) == ["b", "d", "c"]


def test_土台が無い新規ファイルは自分のものになる():
    assert merge(merge_state.MISSING, {"a": 1}, {"b": 2}) == {"b": 2, "a": 1}


def test_記事化の記録が前の実行の記録と併合される(tmp_path):
    """9/29 の 80 件（相手）と 9/30 の 73 件（自分）が両方残る。"""
    base_dir, ours_dir, target_dir = (tmp_path / n for n in ("base", "ours", "target"))
    for d in (base_dir, ours_dir, target_dir):
        (d / "state").mkdir(parents=True)
    base = {"written": {"s_old": {"date": "2026-09-28"}}, "written_articles": {"s_old": ["a"]}}
    ours = json.loads(json.dumps(base))
    ours["written"]["s_new"] = {"date": "2026-09-30"}
    ours["written_articles"]["s_new"] = ["n"]
    theirs = json.loads(json.dumps(base))
    theirs["written"]["s_prev"] = {"date": "2026-09-29"}
    theirs["written_articles"]["s_prev"] = ["p"]
    (base_dir / "state/written.json").write_text(json.dumps(base), encoding="utf-8")
    (ours_dir / "state/written.json").write_text(json.dumps(ours), encoding="utf-8")
    (target_dir / "state/written.json").write_text(json.dumps(theirs), encoding="utf-8")
    (ours_dir / "discovered_sources.yml").write_text("sources: []\n", encoding="utf-8")

    written = merge_state.merge_dirs(base_dir, ours_dir, target_dir)

    merged = json.loads((target_dir / "state/written.json").read_text(encoding="utf-8"))
    assert set(merged["written"]) == {"s_old", "s_prev", "s_new"}
    assert merged["written_articles"]["s_new"] == ["n"]
    # JSON 以外は自分のものをそのまま置く
    assert (target_dir / "discovered_sources.yml").read_text(encoding="utf-8") == "sources: []\n"
    assert len(written) == 2
