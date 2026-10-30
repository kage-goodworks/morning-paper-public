"""xAI（Grok）。X上の反応と探索。

`reactions.x_enabled` が false のとき、反応の取得では呼ばれない。探索の段階1
だけは false でも実行する（記事が集まらないのは例外事態のため）。

**対象の絞り込みとバッチ分割は呼び出し側**（`src/reactions.py`）。ここは
渡されたまとまりに答えるだけ。正規化は `parsers/social.py` の共通関数を通す。
"""

from __future__ import annotations

import json
import os

from ...models import Article, SocialResult
from ..parsers import social as social_parser
from . import http

ENDPOINT = "https://api.x.ai/v1/chat/completions"
MODEL = "grok-4-latest"


def _ask(prompt: str, *, timeout: float = 90.0) -> dict:
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise RuntimeError("XAI_API_KEY が設定されていません")

    payload = http.post_json(
        ENDPOINT,
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "search_parameters": {"mode": "on", "sources": [{"type": "x"}]},
            "response_format": {"type": "json_object"},
        },
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout)

    choices = (payload or {}).get("choices") or []
    if not choices:
        raise RuntimeError("xAI が応答を返しませんでした")
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"xAI の応答をJSONとして読めません: {text[:120]}") from exc
    # citations は応答の外側に載ることがある
    if isinstance(data, dict) and not data.get("citations"):
        data["citations"] = payload.get("citations") or []
    return data if isinstance(data, dict) else {}


class RealSocialClient:
    """`SocialClient`。落ちるとX関連だけが外れる。"""

    def reactions(self, articles: list[Article]) -> dict[str, SocialResult]:
        """まとめて渡された記事について、傾向の要約を取る。

        **個人の投稿本文とアカウント名は出させない**（FR-7 / AC-18）。
        出典が無ければ要約も出させない（創作させないため）。
        """
        if not articles:
            return {}

        listing = "\n".join(
            f'{i + 1}. {a.title}\n   {a.url}' for i, a in enumerate(articles))
        prompt = (
            "次の記事それぞれについて、X上でどのような反応があったかを調べ、"
            "傾向を1〜2文の日本語で要約してください。\n\n"
            "守ること:\n"
            "- 個人のアカウント名と投稿本文は書かない。傾向だけを書く\n"
            "- 出典が見つからない記事は summary を空文字にする。推測で書かない\n\n"
            "次の形式のJSONだけを返してください。\n"
            '{"items": [{"index": 1, "summary": "...", "citations": ["https://..."]}]}\n\n'
            f"{listing}")

        data = _ask(prompt)
        out: dict[str, SocialResult] = {}
        for item in data.get("items") or []:
            try:
                index = int(item.get("index", 0)) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(articles):
                out[articles[index].url] = social_parser.parse(item)
        return out

    def topics(self, date: str) -> list[str]:
        """「昨日Xで話題になったこと」。見出しだけを8〜10項目。"""
        prompt = (
            f"{date} の前日に、X上で技術者の間で話題になったことを"
            "8〜10項目、日本語の短い見出しで挙げてください。\n"
            "個人のアカウント名と投稿本文は書かないでください。\n"
            '次の形式のJSONだけを返してください。{"topics": ["...", "..."]}')
        return [str(t) for t in (_ask(prompt).get("topics") or []) if t][:10]

    def discover(self, date: str) -> list[Article]:
        """記事が集まらないときの逆引き（FR-2 段階1）。

        **Xの投稿そのものは記事にしない。**言及されている一次ソースのURLを
        挙げさせ、採否は呼び出し側が `_is_primary_source` で判断する。
        """
        prompt = (
            f"{date} の前日に、X上で話題になった技術系の出来事について、"
            "言及されている**一次ソース**（公式発表、リリースノート、論文）の"
            "URLを挙げてください。Xの投稿そのものは挙げないでください。\n"
            "次の形式のJSONだけを返してください。\n"
            '{"items": [{"title": "...", "url": "https://...", "category": "ai"}]}\n'
            "category は ai / lang / devtools / infra / industry / security / "
            "gadget / keyboard / career のいずれかにしてください。")

        out: list[Article] = []
        for item in _ask(prompt).get("items") or []:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if not url or not title:
                continue
            out.append(Article(
                id="", url=url, title=title, source="discover_x",
                category=str(item.get("category") or "ai"),
                published_at=date, origin="discover_x"))
        return out
