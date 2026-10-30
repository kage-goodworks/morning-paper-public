"""Claude と Gemini。

LLMを使うのは翻訳・執筆・YouTube動画の要約の3か所だけ（基本方針）。
**降格の判断はここではしない。**`write.model` が失敗したら `fallback_model`
へ落とすのは `src/write.py` の役目で、モックでも本番でも同じ経路を通る。

- Claude は認証の種類で経路が変わる
  - `ANTHROPIC_API_KEY`（従量課金）: 公式の `anthropic` SDK
  - `CLAUDE_CODE_OAUTH_TOKEN`（定額プラン）: **Claude Code の非対話モード
    （`claude -p`）を子プロセスで呼ぶ。**定額プランのトークンは Claude Code
    以外の経路（SDK 直叩き）では 429 が返る（2026-09-15 に診断で確認）。
    公式の claude-code-action と同じ形で、規約上もこちらが正しい
- Gemini は公式SDKを足さず、`http.post_json` で叩く（依存を増やさないため）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from . import http

#: 執筆と週次レビューで使うモデル。
CLAUDE_MODEL = "claude-opus-5"

#: 用途ごとの出力上限。記事は600〜900字が4〜5本なので余裕を見る。
MAX_TOKENS = {"write": 16000, "weekly": 16000, "translate": 4000,
              "youtube": 2000}

GEMINI_MODEL = "gemini-flash-latest"  # 実際の値は config.yml の models.gemini
GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   "{model}:generateContent")
GEMINI_MODELS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"


#: Claude Code の非対話モードに渡す system prompt。道具は使わせない。
CLI_SYSTEM_PROMPT = ("あなたは朝刊パイプラインの執筆担当です。指示された形式の"
                     "本文だけを返し、前置きや補足は付けません。")

#: `claude -p` の待ち時間。16,000トークンの記事執筆を見込む。
CLI_TIMEOUT_SECONDS = 600


class RealClaude:
    """`LLMClient`。執筆と週次レビューを担当する。

    認証は2通り。`ANTHROPIC_API_KEY` があれば SDK を使い、無ければ
    `CLAUDE_CODE_OAUTH_TOKEN`（`claude setup-token` で作るもの）で
    Claude Code の非対話モードを子プロセスで呼ぶ。
    """

    name = "claude"

    def __init__(self, model: str = CLAUDE_MODEL,
                 cli_path: str | None = None) -> None:
        self.model = model
        self.cli_path = cli_path
        self._client = None

    # ---- 経路の選択 ------------------------------------------------------

    def _via_cli(self) -> bool:
        return (not os.environ.get("ANTHROPIC_API_KEY")
                and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")))

    def complete(self, prompt: str, purpose: str) -> str:
        if self._via_cli():
            return self._complete_cli(prompt, purpose)
        return self._complete_sdk(prompt, purpose)

    # ---- 定額プラン: Claude Code の非対話モード ----------------------------

    def _cli(self) -> str:
        path = self.cli_path or os.environ.get("CLAUDE_CLI") or shutil.which("claude")
        if not path:
            raise RuntimeError(
                "claude コマンドが見つかりません（Claude Code が未導入）")
        return path

    def _complete_cli(self, prompt: str, purpose: str) -> str:
        """`claude -p --output-format json` を呼び、`result` を返す。

        - `--tools ""` で道具を使わせない（執筆だけ）
        - `--no-session-persistence` で履歴を残さない
        - 空の一時ディレクトリで動かし、プロジェクト設定や CLAUDE.md を
          読ませない
        - トークンは環境変数のまま渡す。コマンドラインにもログにも出さない
        """
        command = [
            self._cli(), "-p",
            "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            "--model", self.model,
            "--system-prompt", CLI_SYSTEM_PROMPT,
        ]
        with tempfile.TemporaryDirectory(prefix="morning-paper-claude-") as cwd:
            try:
                proc = subprocess.run(
                    command, input=prompt, capture_output=True, text=True,
                    encoding="utf-8", cwd=cwd, timeout=CLI_TIMEOUT_SECONDS,
                    env=os.environ.copy())
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(
                    f"timeout: claude -p が{CLI_TIMEOUT_SECONDS}秒で終わらない") from exc

        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(
                f"claude -p が終了コード {proc.returncode}: "
                f"{_tail(proc.stderr)}")

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude -p の出力がJSONではない: {_tail(proc.stdout)}") from exc
        return _cli_result(payload)

    # ---- 従量課金: 公式 SDK ------------------------------------------------

    def _ensure_client(self):
        if self._client is not None:
            return self._client

        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        self._client = (anthropic.Anthropic(api_key=api_key) if api_key
                        else anthropic.Anthropic())
        return self._client

    def _complete_sdk(self, prompt: str, purpose: str) -> str:
        client = self._ensure_client()
        max_tokens = MAX_TOKENS.get(purpose, 8000)

        # 長い出力はストリーミングで受ける。HTTPのタイムアウトに当たらないため。
        with client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()

        if message.stop_reason == "refusal":
            detail = getattr(message.stop_details, "category", None)
            raise RuntimeError(f"Claude が応答を拒否しました（{detail}）")

        return "".join(block.text for block in message.content
                       if block.type == "text").strip()


def _cli_result(payload: dict) -> str:
    """`claude -p --output-format json` の応答から本文を取り出す。純粋関数。

    `is_error` か `stop_reason == "refusal"` は例外にし、`retry.call` の
    縮退（Gemini への降格）へ乗せる。
    """
    if not isinstance(payload, dict):
        raise RuntimeError("claude -p の出力が想定の形ではない")
    if payload.get("is_error"):
        raise RuntimeError(
            f"claude -p がエラーを返しました（{payload.get('subtype')}: "
            f"{_tail(str(payload.get('result') or ''))}）")
    if payload.get("stop_reason") == "refusal":
        raise RuntimeError("Claude が応答を拒否しました（refusal）")
    return str(payload.get("result") or "").strip()


def _tail(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text[-limit:]


class RealGemini:
    """`LLMClient`。翻訳・YouTube要約・執筆の降格先。

    YouTubeの要約は動画URLをそのまま渡す（動画をダウンロードしない）。
    """

    name = "gemini"

    def __init__(self, model: str = GEMINI_MODEL,
                 fallbacks: list[str] | None = None) -> None:
        self.model = model
        #: 本命が 404（引退）や 503（混雑）のとき順に試す候補
        self.fallbacks = list(fallbacks or [])

    def complete(self, prompt: str, purpose: str) -> str:
        """本命 → 候補の順に試す。全部だめなら最後の例外を投げる。

        モデルの引退（404）と混雑（503）は同じモデルに再試行しても直らない
        ことが多い。`retry.call` の3回はそのまま、その内側で候補を渡り歩く。
        """
        tried: list[str] = []
        for model in [self.model, *self.fallbacks]:
            try:
                return self._complete_with(model, prompt, purpose)
            except http.FetchError as exc:
                tried.append(f"{model}: {str(exc)[:120]}")
                if not any(code in str(exc) for code in ("HTTP 404", "HTTP 503",
                                                        "HTTP 429")):
                    raise
        # 全候補の理由を残す。最後の1つだけだと本命が何で落ちたか分からない
        raise http.FetchError("全候補が失敗 / " + " / ".join(tried))

    def _complete_with(self, model: str, prompt: str, purpose: str) -> str:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY が設定されていません")

        parts: list[dict] = []
        if purpose == "youtube":
            url = _first_url(prompt)
            if url:
                parts.append({"fileData": {"fileUri": url}})
        parts.append({"text": prompt})

        payload = http.post_json(
            GEMINI_ENDPOINT.format(model=model),
            {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "maxOutputTokens": MAX_TOKENS.get(purpose, 8000),
                },
            },
            headers={"x-goog-api-key": key},
            timeout=120.0)

        candidates = (payload or {}).get("candidates") or []
        if not candidates:
            feedback = (payload or {}).get("promptFeedback") or {}
            raise RuntimeError(f"Gemini が応答を返しませんでした（{feedback}）")

        chunks = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(str(c.get("text") or "") for c in chunks).strip()

    def list_models(self) -> list[str]:
        """呼べるモデル名の一覧。診断用。モデルが引退したときに次を選ぶため。"""
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY が設定されていません")
        payload = http.fetch_json(GEMINI_MODELS_ENDPOINT,
                                  headers={"x-goog-api-key": key}, timeout=30.0)
        names = []
        for model in (payload or {}).get("models") or []:
            name = str(model.get("name") or "").removeprefix("models/")
            methods = model.get("supportedGenerationMethods") or []
            if name and "generateContent" in methods:
                names.append(name)
        return names


def _first_url(text: str) -> str:
    for token in (text or "").split():
        if token.startswith("http://") or token.startswith("https://"):
            return token.rstrip(".,;)")
    return ""
