"""設定の読み込み。既定値をコードへ持たない（NFR-7）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, data: dict[str, Any], root: Path = ROOT) -> None:
        self._d = data
        self.root = root

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, path: str, default: Any = None) -> Any:
        """`limits.per_category` のようにドット区切りで引く。"""
        cur: Any = self._d
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def data(self) -> dict[str, Any]:
        return self._d


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load(root: Path = ROOT) -> Config:
    data = _load_yaml(root / "config.yml")
    data["handles"] = _load_yaml(root / "handles.yml").get("allowed_x_handles", [])
    data["youtube"] = _load_yaml(root / "youtube.yml").get("channels", [])
    return Config(data, root)
