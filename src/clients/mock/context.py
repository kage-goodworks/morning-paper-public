"""モック実行の文脈。

「いま何日目か」「どのシナリオか」「出力をどこへ書くか」を1か所に持つ。
各モックはここを見るだけで日ごとの状態変化を再現でき、個々のモックへ
シナリオの分岐を書かずに済む。
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


class MockContext:
    def __init__(self, *, scenario: str = "normal", day: int = 1,
                 out_dir: Path | None = None, phase: str = "morning",
                 fixtures: Path = FIXTURES) -> None:
        self.fixtures = fixtures
        #: "morning" か "weekly"。週次は反応を取り直す工程なので、
        #: モックが返す値もそれに合わせる。
        self.phase = phase
        self.out = out_dir or (ROOT / "out")
        self.scenario = scenario
        self.day = day
        self._scenarios = self._load_scenarios()
        self.seq = 0

    def _load_scenarios(self) -> dict:
        path = self.fixtures / "scenarios.yml"
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @property
    def definition(self) -> dict:
        return self._scenarios.get(self.scenario, {})

    @property
    def feed_dir(self) -> Path:
        """その日に読むフィードのディレクトリ。"""
        feeds = self.definition.get("feeds") or {}
        name = feeds.get(self.day) or f"day{self.day}"
        return self.fixtures / "rss" / str(name)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def read_bytes(self, *parts: str) -> bytes | None:
        path = self.fixtures.joinpath(*parts)
        return path.read_bytes() if path.exists() else None

    def read_text(self, *parts: str) -> str | None:
        path = self.fixtures.joinpath(*parts)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def read_json(self, *parts: str):
        import json
        text = self.read_text(*parts)
        return json.loads(text) if text is not None else None
