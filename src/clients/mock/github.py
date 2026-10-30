"""GitHub Releases のモック。Real と同じ `parsers.releases.parse` を通す。"""

from __future__ import annotations

from ...models import Article
from ..parsers import releases as releases_parser
from .context import MockContext


class MockReleaseClient:
    """`ReleaseClient` のモック。"""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def fetch(self, repo: str) -> list[Article]:
        payload = self.ctx.read_json("releases", "releases.json") or []
        return releases_parser.parse(payload, repo=repo, category="lang")
