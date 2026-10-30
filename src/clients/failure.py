"""失敗注入。

**個々のモックに失敗の分岐を書かない。**どのクライアントにも同じラッパーを
かぶせられるようにして、`--fail` の指定だけで挙動を変える。実装が増えても
失敗系の再現方法は1か所のままになる。エラー処理は正常系より壊れやすいので、
モックから踏めることを設計に含める。

再現できる形:

- `always`     毎回失敗する（3回のリトライを使い切る）
- `transient`  既定2回失敗して3回目に成功する
- `timeout`    TimeoutError を投げる
- `empty`      例外を投げず、空の結果を返す
- `missing`    一部フィールドが欠けた値を返す
- `badformat`  LLM出力の形式が不正な文字列を返す
- `credit`     クレジット切れとして失敗する（通知の文面を分けるため）
"""

from __future__ import annotations

from typing import Any, Callable

MODES = ("always", "transient", "timeout", "empty", "missing",
         "badformat", "credit")

#: 個別名から Clients のスロットへの対応。`--fail stooq` のように
#: サービス名で書けるようにする。
ALIASES = {
    "stooq": "quotes",
    "hn": "reactions",
    "hatena": "reactions",
    "reddit": "reactions",
    "grok": "social",
    "vault": "notes",
    "github": "releases",
    "rss": "feed",
    "firecrawl": "search",
}


class InjectedFailure(RuntimeError):
    """注入された失敗。実際の障害と同じ経路でリトライ・縮退へ流す。"""

    def __init__(self, target: str, mode: str, status: int | None = None) -> None:
        self.target = target
        self.mode = mode
        self.status = status
        if status == 503:
            detail = "HTTP 503 Service Unavailable"
        elif mode == "credit":
            detail = "credit balance is too low"
        else:
            detail = mode
        super().__init__(f"{target}: {detail}")


class FailureSpec:
    """`--fail` の指定を解釈した結果。

    `stooq` のように名前だけなら `always`。`slack:transient` のようにコロンで
    モードを指定できる。
    """

    def __init__(self, raw: str | None = None, transient_fails: int = 2) -> None:
        self.rules: dict[str, str] = {}
        self.transient_fails = transient_fails
        for token in (raw or "").split(","):
            token = token.strip()
            if not token:
                continue
            name, _, mode = token.partition(":")
            mode = mode or "always"
            if mode not in MODES:
                raise ValueError(
                    "未知の失敗モード: " + mode + "（" + "/".join(MODES) + "）")
            self.rules[name.strip()] = mode

    def mode_for(self, target: str) -> str | None:
        return self.rules.get(target)

    def __bool__(self) -> bool:
        return bool(self.rules)


EMPTY_BY_TYPE: dict[type, Callable[[], Any]] = {list: list, dict: dict, str: str}


class FailingProxy:
    """任意のクライアントを包む汎用プロキシ。

    メソッド呼び出しを横取りし、モードに応じた失敗を起こす。包む対象が
    Mock か Real かを問わない。
    """

    def __init__(self, inner: Any, target: str, mode: str,
                 transient_fails: int = 2) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_transient_fails", transient_fails)
        object.__setattr__(self, "_calls", 0)

    def __getattr__(self, item: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, item)
        if not callable(attr):
            return attr

        target = object.__getattribute__(self, "_target")
        mode = object.__getattribute__(self, "_mode")
        limit = object.__getattribute__(self, "_transient_fails")

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            calls = object.__getattribute__(self, "_calls") + 1
            object.__setattr__(self, "_calls", calls)

            if mode == "always":
                raise InjectedFailure(target, mode, 503)
            if mode == "credit":
                # 実際のAPIが返す文言に合わせた確認は実キー投入の段階で行う。
                raise InjectedFailure(target, mode)
            if mode == "timeout":
                raise TimeoutError(target + ": 応答がありません（注入）")
            if mode == "transient":
                if calls <= limit:
                    raise InjectedFailure(target, mode, 503)
                return attr(*args, **kwargs)
            if mode == "empty":
                return empty_like(attr(*args, **kwargs))
            if mode == "missing":
                return drop_fields(attr(*args, **kwargs))
            if mode == "badformat":
                return "この応答には区切りがありません。"
            return attr(*args, **kwargs)

        return wrapped


def empty_like(value: Any) -> Any:
    """同じ型の空値を返す。呼び出し側の型を壊さないため。"""
    factory = EMPTY_BY_TYPE.get(type(value))
    if factory is not None:
        return factory()
    if hasattr(value, "__dataclass_fields__"):
        return value.__class__()
    return None


def drop_fields(value: Any) -> Any:
    """一部フィールドが欠けた状態を作る。パーサの欠損耐性を踏むため。"""
    if isinstance(value, list):
        return [drop_fields(v) for v in value[: max(1, len(value) // 2)]]
    if isinstance(value, dict):
        keys = list(value)
        return {k: v for k, v in value.items() if k != (keys[0] if keys else None)}
    if hasattr(value, "__dataclass_fields__"):
        for name in ("title", "url", "body_md", "summary"):
            if hasattr(value, name):
                setattr(value, name, "")
                break
    return value


def wrap(clients: Any, spec: FailureSpec) -> Any:
    """`Clients` の各スロットへ、指定された失敗モードのプロキシをかける。

    `extractors` と `llms` は複数実装を持つため、それぞれの `name` で個別に包む。
    """
    if not spec:
        return clients

    slots = ("feed", "reactions", "releases", "quotes", "social",
             "search", "slack", "notes")
    for slot in slots:
        mode = spec.mode_for(slot)
        if mode:
            setattr(clients, slot, FailingProxy(
                getattr(clients, slot), slot, mode, spec.transient_fails))

    for name, slot in ALIASES.items():
        mode = spec.mode_for(name)
        if mode:
            setattr(clients, slot, FailingProxy(
                getattr(clients, slot), name, mode, spec.transient_fails))

    clients.extractors = [
        FailingProxy(e, e.name, spec.mode_for(e.name), spec.transient_fails)
        if spec.mode_for(e.name) else e
        for e in clients.extractors
    ]
    clients.llms = {
        key: (FailingProxy(impl, key, spec.mode_for(key), spec.transient_fails)
              if spec.mode_for(key) else impl)
        for key, impl in clients.llms.items()
    }
    return clients
