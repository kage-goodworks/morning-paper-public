"""GitHub Releases の取得。`GITHUB_TOKEN` があればレート制限が緩くなる。"""

from __future__ import annotations

import os

from ...models import Article
from ..parsers import releases as releases_parser
from . import http

ENDPOINT = "https://api.github.com/repos/{repo}/releases"


class RealReleaseClient:
    """`ReleaseClient`。取得JSONを `parsers.releases.parse` へ渡す。"""

    def __init__(self, per_page: int = 5) -> None:
        self.per_page = per_page

    def fetch(self, repo: str) -> list[Article]:
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = ENDPOINT.format(repo=repo) + f"?per_page={self.per_page}"
        payload = http.fetch_json(url, timeout=20.0, headers=headers)
        return releases_parser.parse(payload, repo=repo, category="lang")
