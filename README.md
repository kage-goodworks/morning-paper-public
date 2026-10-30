# morning-paper（朝刊パイプライン）

毎朝 IT ニュースの見出し一覧を Slack へ送り、選んだ話題だけを解説記事の紙面（PDF）にして返す自動化。

## 仕組み

| 役割 | 場所 |
|---|---|
| 収集・記事化・紙面づくり | GitHub Actions（このリポジトリ） |
| 時計（毎朝 04:40 JST）と Slack ボタンの中継 | Cloudflare Workers（`workers/`） |
| 執筆 | Claude Code（`claude -p`）。落ちたら Gemini |
| 知識ノート | Obsidian Vault のリポジトリへ deploy key で push |

## 公開してよいもの・しないもの

このリポジトリにはプログラムと情報源の設定だけを置く。次は**置かない**。

- 読み手の像（記事の「自分との関係」の材料）、既読・投稿の記録 … 非公開の別リポジトリ（Actions が `private/` に取り込む。`DATA_DEPLOY_KEY`）
- 紙面そのもの … Slack にだけ届ける。やり直し用の控えは暗号化して成果物に残す（7日）
- 鍵 … GitHub Secrets
- 実行ログの失敗理由 … 見出しだけ出す（`MORNING_PAPER_PUBLIC_LOG`、`src/pipeline.py`）

## Secrets

| 名前 | 必須 | 用途 |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | ○ | 執筆（`claude setup-token`） |
| `GEMINI_API_KEY` | ○ | 翻訳・要約・執筆の予備 |
| `SLACK_BOT_TOKEN` | ○ | 一覧と紙面の投稿 |
| `VAULT_DEPLOY_KEY` | ○ | 知識ノートの push |
| `DATA_DEPLOY_KEY` | ○ | 非公開データの読み書き・紙面の控えの暗号化 |
| `YOUTUBE_API_KEY` / `FIRECRAWL_API_KEY` / `XAI_API_KEY` / `ANTHROPIC_API_KEY` | 任意 | 無くても縮退して動く |

## 開発

Python 3.12。

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -m pytest              # 外部通信なしの自動テスト
.venv\Scripts\python.exe -m src.simulate full   # モックで3日分を通す
```

Workers: `cd workers && node --test && npx wrangler@4 deploy`
