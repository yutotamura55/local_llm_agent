# Local LLM Agent

Ollama上で動くローカルLLMに、ファイルシステム操作(閲覧・読み込み・書き込み・編集・コマンド実行)をさせ、ブラウザから操作できるエージェント。Claude Codeのようなコーディングエージェントの内部動作を、自分の手で組み立てることで理解するための学習プロジェクト。

設計判断の理由や検証で得た知見など、経緯にあたる内容は[`docs/DESIGN.md`](docs/DESIGN.md)と[`docs/FINDINGS.md`](docs/FINDINGS.md)にまとめている。

## できること

読み取り・書き込み・コマンド実行の5種類のツールをモデルに持たせ、Function Calling経由で呼び出せる。

| ツール | 内容 | 安全対策 |
|---|---|---|
| `list_files` | 指定ディレクトリのファイル一覧を取得 | 操作範囲を許可済みワークスペース(BASE_DIR)配下に限定 |
| `read_file` | 指定ファイルの中身を読み込む | 同上 |
| `write_file` | 指定ファイルに書き込む(新規作成・上書き) | 同上に加え、実行前にユーザーの確認を必須化 |
| `execute_command` | シェルコマンドを実行 | 作業ディレクトリ(cwd)をBASE_DIRに固定。読み取り専用コマンド(`ls`, `git status`等)は即実行、変更を伴う可能性のあるコマンドのみ確認を必須化。タイムアウト30秒 |
| `edit_file` | ファイル内のテキストを厳密一致・単一置換で書き換え | 操作範囲をBASE_DIR配下に限定。置換対象の文字列がファイル内で一意でない場合はエラーとし、実行しない。実行前にdiffプレビューで確認を必須化 |

ブラウザ(Nuxt4製UI)から、使用するモデルの選択・操作対象ワークスペースの選択・プロンプトの入力・実行結果の確認・確認(confirm)操作までを一通り行える。

## アーキテクチャ

```
ブラウザ(Nuxt4)
   │  モデル選択 / ワークスペース選択 / プロンプト入力
   ▼
FastAPI バックエンド
   │  ワークスペース名 → BASE_DIR(実パス)に解決
   │  (BASE_DIRは事前登録された許可リストからのみ選択可能。自由入力は不可)
   ▼
Ollama(LLM) ──tools定義を渡す──► モデルが「使いたい道具」を意思表示 (tool_calls)
   │                                    │
   │                        content=空、tool_callsに関数名と引数
   ▼                                    ▼
バックエンド側で検証・実行(実際の処理はコードが担当。モデルは何も実行しない)
   │  ファイル操作系ツールは resolve_safe_path() で
   │  対象パスがBASE_DIR配下に実際に収まっているか検証してから実行
   │
   │  確認が必要な操作(write_file / edit_file / 変更を伴うexecute_command)は
   │  pending_id を発行してブラウザに確認UIを返し、承認後に再開する
   ▼
実行結果をtool roleのメッセージとして再度Ollamaに送信
   │
   ▼
モデルが結果を踏まえて自然文で最終回答を生成 → ブラウザに表示
```

設計判断の理由は[`docs/DESIGN.md`](docs/DESIGN.md)を参照。

## セットアップ

### バックエンド(FastAPI + Ollama)

`main.py` / `agent_tools.py` はプロジェクトルート直下に置き、`test_tool.py`と同じ1つのuv環境で管理する構成になっている(`backend/`のようなサブディレクトリには分けていない)。

```bash
# Ollamaのインストール(Linux/WSL)
curl -fsSL https://ollama.com/install.sh | sh

# モデルの取得
ollama pull qwen2.5:7b

# プロジェクトルートで実行
uv add fastapi "uvicorn[standard]" ollama
uv run uvicorn main:app --reload --port 8000
```

起動前に、`agent_tools.py`内の`ALLOWED_WORKSPACES`にエージェントが操作してよいディレクトリ(実パス)を登録しておく必要がある。

```python
ALLOWED_WORKSPACES: dict[str, str] = {
    "local_llm_agent": "/path/to/local_llm_agent",
}
```

### フロントエンド(Nuxt4)

```bash
cd frontend
bun install
bun run dev
```

`http://localhost:3000` にアクセスすると、モデル選択・プロンプト入力・結果確認のUIが表示される。

### CLIでの検証スクリプト(モデル比較用)

```bash
uv run test_tool.py
```

`test_tool.py`はブラウザUIとは別に、単一モデルでのツール呼び出し精度をCSVログ付きで検証するための実験用スクリプトとして引き続き利用している。検証で得た知見は[`docs/FINDINGS.md`](docs/FINDINGS.md)にまとめている。

## 動作確認環境

- Windows 11 + WSL2
- CPU: 13th Gen Intel Core i7-13700K
- RAM: 32GB
- GPU VRAM: 8GB
- Ollama: 0.33.3

## 今後の展望

- 複数ツール呼び出しの逐次処理対応(現状は1回のリクエストにつき1ツールのみ処理)
- 複数ターンの会話履歴保持
- `execute_command`のOSレベルサンドボックス化(現状はcwd固定のみで、絶対パス指定によるBASE_DIR外への読み取り・書き込みは防げていない)
- ブラウザアクセス時のパスキーによるユーザー認証機能
- ECSでのデプロイを想定したコンテナ化
- CI/CDパイプラインの作成
- タスク別(ツール選択用/要約用/コード生成用)の推奨モデル一覧の整備

## License

MIT
