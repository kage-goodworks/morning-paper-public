"""ワークフローの定義が YAML として読めることを確かめる。

2026-10-01 に `run:` の行へ「: 」を含む文字列を素で書いて構文エラーにし、
朝の処理が起動しなかった（GitHub は不正なワークフローを黙って走らせない）。
push の前に必ず落ちるよう、ここで固定する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_ワークフローはYAMLとして読め_ジョブとステップを持つ(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("jobs"), path.name
    for job in data["jobs"].values():
        steps = job.get("steps") or []
        assert steps, f"{path.name}: steps が無い"
        for step in steps:
            assert "run" in step or "uses" in step, f"{path.name}: {step.get('name')}"
            if "run" in step:
                assert isinstance(step["run"], str) and step["run"].strip()


def test_stateを書く3つのワークフローは併合スクリプトでpushする():
    for name in ("morning", "articles", "weekly"):
        path = next(p for p in WORKFLOWS if p.stem == name)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        runs = [s["run"] for job in data["jobs"].values() for s in job["steps"] if "run" in s]
        assert any("scripts/push_state.sh" in r for r in runs), name
        assert not any("git rebase" in r for r in runs), name
