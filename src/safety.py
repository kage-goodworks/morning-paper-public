"""外部から来た値を内部構造へ入れる前の検査。

**フィードの中身は信用できない。**タイトル・URL・日時・カテゴリはすべて
外部が決める値で、そのまま使うとファイルの置き場所、Vaultノートの
フロントマター、Slackのリンクを外から操作できてしまう。

ここに置くのは全部純粋関数で、`tests/test_safety.py` が対象。
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# パスの組み立て
# --------------------------------------------------------------------------

_SEGMENT_OK = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
_YEAR_OK = re.compile(r"\A[0-9]{4}\Z")
_MONTH_OK = re.compile(r"\A(0[1-9]|1[0-2])\Z")
_DAY_OK = re.compile(r"\A[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])\Z")
_ID_OK = re.compile(r"\A[0-9a-f]{1,32}\Z")

UNKNOWN_SEGMENT = "unknown"
UNKNOWN_DATE = "0000-00-00"


def safe_segment(value: str, *, allowed: set[str] | None = None) -> str:
    """パスの1区切りとして使える形か検査する。

    `..` や `/` が混ざった値をそのまま連結すると、Vaultの想定外の場所へ
    書き込める。カテゴリは探索（Grokや検索連携の応答）由来にもなるため、
    外部が決める値として扱う。
    """
    text = (value or "").strip().lower()
    if allowed is not None:
        return text if text in allowed else UNKNOWN_SEGMENT
    return text if _SEGMENT_OK.match(text) else UNKNOWN_SEGMENT


def safe_date(value: str) -> str:
    """`YYYY-MM-DD` として妥当な先頭10文字だけを返す。

    フィードの日時は形式がばらばらで、`../../..` のような値も来うる。
    """
    text = (value or "")[:10]
    return text if _DAY_OK.match(text) else UNKNOWN_DATE


def safe_id(value: str) -> str:
    """記事idは16進のハッシュ。それ以外は受け付けない。"""
    text = (value or "").strip().lower()
    return text if _ID_OK.match(text) else UNKNOWN_SEGMENT


# --------------------------------------------------------------------------
# フロントマター
# --------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def yaml_scalar(value: str) -> str:
    """フロントマターの値として安全な1行にする。

    改行が残ると、その後ろが新しいキーとして解釈される。`vault: private` を
    差し込まれると、そのノートはOpen Vaultの対象から外れて見えなくなる。
    """
    text = _CONTROL.sub(" ", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return '""'
    needs_quote = (
        text[0] in "-?:,[]{}#&*!|>'\"%@`"
        or ": " in text
        or text.endswith(":")
        or text.lower() in ("true", "false", "null", "yes", "no", "on", "off")
    )
    if needs_quote:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def yaml_unscalar(value: str) -> str:
    """`yaml_scalar` の逆。引用符で囲まれていれば外し、エスケープを戻す。"""
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        inner = text[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return text


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------

def slack_escape(text: str) -> str:
    """Slackのmrkdwnで特別な意味を持つ文字を落とす。

    リンクは `<URL|表示名>` と書く。表示名に `>` が入ると**そこでリンクが
    終わり、続きが別のリンクとして描画される。**フィードのタイトルは外部が
    決めるので、これを許すと任意のリンクをSlackへ出せてしまう。

    Slackの仕様どおり `&` `<` `>` の3つだけを実体参照にする。
    """
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def markdown_link(text: str, url: str) -> str:
    """`[表示名](URL)` を壊れない形で組み立てる。"""
    label = str(text or "").replace("[", "［").replace("]", "］")
    label = _CONTROL.sub(" ", label).strip()
    target = _CONTROL.sub("", str(url or "")).strip()
    target = target.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    return f"[{label}]({target})"


# --------------------------------------------------------------------------
# 取得先のURL
# --------------------------------------------------------------------------

ALLOWED_SCHEMES = ("http", "https")

#: 取りに行ってはいけない宛先。クラウドのメタデータ、内部ネットワーク、
#: ループバック。フィードやGrokの応答に混ぜられると、内部だけに見える
#: 情報を外から引き出す踏み台にされる（SSRF）。
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata",
                 "instance-data"}


def is_fetchable(url: str) -> bool:
    """取りに行ってよいURLか。

    **`real/` の実装はフィードやGrokが返したURLを取りに行く前にこれを通す。**
    ホスト名がDNSで内部アドレスへ解決される場合までは判定できないので、
    実装側では名前解決のあとにも同じ検査を行うこと。
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return False

    host = (parts.hostname or "").lower()
    if not host or host in BLOCKED_HOSTS:
        return False
    if host.endswith(".localhost") or host.endswith(".internal"):
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # ホスト名。解決後に実装側で再検査する

    return not (address.is_private or address.is_loopback
                or address.is_link_local or address.is_reserved
                or address.is_multicast or address.is_unspecified)
