"""外部クライアントの抽象インタフェース。

**ベンダー単位ではなく、能力・縮退単位で分ける。**「Hacker News クライアント」
ではなく「無料で取れる反応源」で括る。落ちたときに何が欠けるかが縮退の単位で
あり、インタフェースの境界をそこへ合わせると呼び出し側の分岐が1つで済む。

戻り値は `src/models.py` のドメイン型だけ。HTTP・ファイル・SDKの都合を
呼び出し側へ漏らさない。Mock と Real は同じシグネチャを実装し、レスポンスの
正規化は双方が `parsers/` の共通関数を通る（重複させない）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..models import Article, ExtractResult, Quote, Reactions, SocialResult


@runtime_checkable
class FeedClient(Protocol):
    """RSS / Atom / YouTubeチャンネルRSS。落ちるとそのソースが欠ける。"""

    def fetch_articles(self, source: dict[str, Any]) -> list[Article]: ...


@runtime_checkable
class ReactionClient(Protocol):
    """無料で取れる反応（HN・はてブ・Reddit）。落ちても反応量0で続行する。"""

    def fetch(self, article: Article) -> Reactions: ...


@runtime_checkable
class BodyExtractor(Protocol):
    """本文取得の1段。4段のフォールバックは呼び出し側が順に試す。

    `name` はその段の名前で、`extracted_by` に記録される。
    """

    name: str

    def extract(self, article: Article) -> ExtractResult: ...


@runtime_checkable
class ReleaseClient(Protocol):
    """GitHub Releases。落ちるとそのリポジトリが欠ける。"""

    def fetch(self, repo: str) -> list[Article]: ...


@runtime_checkable
class QuoteClient(Protocol):
    """株価。落ちると株価帯が消えるだけで、一覧そのものは成立する。"""

    def fetch(self, symbols: list[str]) -> list[Quote]: ...


@runtime_checkable
class LLMClient(Protocol):
    """翻訳・執筆・YouTube要約。LLMを使うのはこの3か所だけ。

    `name` は降格の記録に使う（claude から gemini へ）。
    """

    name: str

    def complete(self, prompt: str, purpose: str) -> str: ...


@runtime_checkable
class SocialClient(Protocol):
    """X（Grok）。x_enabled が false のとき呼ばれない。落ちるとX関連だけ外れる。"""

    def reactions(self, articles: list[Article]) -> dict[str, SocialResult]: ...

    def topics(self, date: str) -> list[str]: ...

    def discover(self, date: str) -> list[Article]: ...


@runtime_checkable
class SearchClient(Protocol):
    """探索（Gemini検索連携 / Firecrawl search）。FR-2 段階2。"""

    def search(self, query: str, date: str) -> list[Article]: ...


@runtime_checkable
class SlackClient(Protocol):
    """投稿・スレッド返信・画像・chat.update・失敗通知。"""

    def post_message(self, blocks: list[dict], text: str) -> dict: ...

    def reply_in_thread(self, thread_ts: str, blocks: list[dict], text: str) -> dict: ...

    def upload_image(self, thread_ts: str, path: str, title: str) -> dict: ...

    def upload_files(self, paths: list[str], text: str,
                     thread_ts: str = "", channel: str | None = None) -> dict: ...

    def update_message(self, ts: str, blocks: list[dict], text: str) -> dict: ...

    def notify_failure(self, report: str) -> dict: ...

    def find_message(self, text: str, *, within_hours: float = 36.0,
                     channel: str | None = None) -> dict | None:
        """同じ本文（`text`）の自分の投稿が直近にあれば {"ts", "permalink"} を返す。

        投稿の直後に応答が届かなかった／プロセスが死んだ、のあとの再実行で
        二重に投稿しないための問い合わせ。無ければ None。
        """
        ...


@runtime_checkable
class NoteStore(Protocol):
    """Vaultへの書き込み。deploy key での commit と push。"""

    def write(self, rel_path: str, content: str) -> None: ...

    def read(self, rel_path: str) -> str | None: ...

    def commit_push(self, message: str) -> None: ...


class Clients:
    """注入される依存の束。呼び出し側はこれだけを受け取り、実装を知らない。"""

    def __init__(self, *, feed: FeedClient, reactions: ReactionClient,
                 extractors: list[BodyExtractor], releases: ReleaseClient,
                 quotes: QuoteClient, llms: dict[str, LLMClient],
                 social: SocialClient, search: SearchClient,
                 slack: SlackClient, notes: NoteStore) -> None:
        self.feed = feed
        self.reactions = reactions
        self.extractors = extractors
        self.releases = releases
        self.quotes = quotes
        self.llms = llms
        self.social = social
        self.search = search
        self.slack = slack
        self.notes = notes
