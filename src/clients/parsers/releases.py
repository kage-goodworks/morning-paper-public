"""GitHub Releases の正規化。純粋関数。"""

from __future__ import annotations

from typing import Any

from ...models import Article


def parse(payload: list[dict[str, Any]], repo: str, category: str = "lang") -> list[Article]:
    out: list[Article] = []
    for rel in payload or []:
        name = rel.get("name") or rel.get("tag_name")
        url = rel.get("html_url")
        if not name or not url:
            continue
        out.append(Article(
            id="", url=url, title=f"{repo} {name}", source="github_releases",
            category=category, published_at=rel.get("published_at") or "",
            origin="config", body_md=(rel.get("body") or "").strip(),
            extracted_by="rss" if rel.get("body") else "none",
        ))
    return out
