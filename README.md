# llm-launcher

vLLM / SGLang / llama.cpp / FreeToken を **サブモジュールとして `module/` 配下に登録** し、
Web UI から **起動プロファイル管理・ワンタップ起動・ログ確認・停止** まで行える管理アプリです。
スマホ (同一 LAN) からもブラウザで操作できます。

## 構成

```
llm-launcher/
├── module/               # バックエンド群 (git submodule)
│   ├── vllm/             # https://github.com/vllm-project/vllm
│   ├── sglang/           # https://github.com/sgl-project/sglang
│   ├── llama.cpp/        # https://github.com/ggml-org/llama.cpp
│   └── freetoken/        # https://github.com/FlashML-org/FreeToken
├── src/llm_launcher/     # 管理アプリ本体 (FastAPI + 静的 SPA)
│   ├── app.py            # REST API
│   ├── process.py        # プロセス起動/監視/停止 (ログ・health probe・detached 引き継ぎ)
│   ├── backends.py       # バックエンド定義・引数テンプレート
│   └── static/index.html # Web UI (PC / スマホ両対応)
└── data/                 # 実行データ (gitignore 済み): プロファイル / 起動履歴 / ログ
```

## セットアップ

```bash
# 1. 依存インストール + サブモジュール取得 (clone した直後)
uv sync
git submodule update --init --depth 1

# 2. 管理アプリを起動 (既定で 0.0.0.0:8036 で待ち受け)
uv run llm-launcher
```

起動時に表示される `http://<LAN-IP>:8036` にスマホ / PC のブラウザからアクセスしてください。

- `--host` / `--port` オプション、または `LLM_LAUNCHER_HOST` / `LLM_LAUNCHER_PORT` 環境変数で変更可能
- バックエンド自体のインストールは各自の環境に依存します:
  - vLLM / SGLang / FreeToken: `uv pip install vllm` など (既定ではプロジェクト `.venv` の python を使用。バックエンド画面で python パスを上書きできます)
  - llama.cpp: `module/llama.cpp` をビルドすると `build/bin/llama-server` を自動検出。PATH 上の `llama-server` も対象。ビルド前でもバックエンド画面でパスを直接指定できます

```bash
cd module/llama.cpp && cmake -B build -DGGML_CUDA=ON && cmake --build build -j
```

## 使い方 (Web UI)

| タブ | できること |
| --- | --- |
| 🚀 起動 | 起動サーバーの選択画面。プロファイルカード内で **バックエンドをタップ切り替え** して起動 (同一プロファイルの複数バックエンド同時起動可)。バックエンド別フィルタ、稼働中サマリー付き |
| 🖥️ サーバー | 起動中 + 履歴の一覧。ステータス (起動中 / 稼働中 / 停止 / 異常終了)、PID・稼働時間・API URL、停止 / 再起動 (元のプロファイル+バックエンドを維持)、ライブログ表示 (追従・自動スクロール) |
| 📋 プロファイル | **バックエンド選択そのものを含む** 起動テンプレート。1プロファイルに vLLM/SGLang/llama.cpp/FreeToken を複数紐付けでき、各バックエンドの特色に合わせた引数・環境変数・追加フラグ・既定起動先 ★ を個別に保持。保存前にバックエンド別プレビュー |
| ⚙️ バックエンド | 各 submodule の状態 (取得済み / インストール検出)、起動コマンドテンプレートや python / バイナリパスの上書き設定、再検出 |

### コマンド組み立ての仕組み

バックエンドごとに最終 argv = コマンドテンプレート (プロファイル設定 > バックエンド設定 > 既定)
+ フォーム値から生成した `--flag 群` + 追加引数

- テンプレート内トークン: `{python}` `{llama_server}` `{module}` `{root}`、および `{model}` `{port}` などプロファイル変数
- 空欄の項目はフラグ自体が付きません
- 「コマンドのみモード」(高度な設定): ON にするとフォーム由来のフラグ自動付与を止めます。フラグ体系の違うカスタムサーバーや位置引数型の起動にも対応
- 旧形式 (単一バックエンド) の `data/profiles.json` は起動時に自動で v2 (複数バックエンド) へ移行されます。API も旧 JSON ボディ (`backend`/`values` 直置き) を引き続き受理します

## API (スクリプト等からも操作可能)

- `GET /api/state` — 全体状態 (バックエンド / プロファイル / セッション)
- `GET/POST/PUT/DELETE /api/profiles[/{id}]` — プロファイル CRUD。ボディは `{name, note, default_backend, configs: {vllm: {values, extra_args, env, cwd, command, custom_only}, ...}}` (v1 単一バックエンド形式も互換受理)
- `POST /api/sessions` `{profile_id, backend?, force?}` — 起動 (backend 省略 = プロファイル既定。ポート衝突・同一 profile+backend 二重起動は 409、`force` でポートのみ回避可)
- `POST /api/sessions/{id}/stop` / `restart`、`GET /api/sessions/{id}/log?cursor=` — 停止 / 再起動 / ログ
- `PUT /api/backends/{id}`、`POST /api/backends/{id}/detect` — バックエンド設定・再検出
- OpenAPI ドキュメント: `GET /api/docs`

## 動作仕様メモ

- バックエンドは `start_new_session` でプロセスグループ化して起動。停止はグループへ SIGTERM → 15 秒後に SIGKILL
- llm-launcher 本体を再起動しても生存中のサーバーは「引き継ぎ (detached)」として検出し、停止・ログ閲覧を継続できます
- `--port` / `-p` / `{port}` からポートを自動解釈し、`GET /health` を 3 秒間隔でプローブ → 緑の「稼働中」バッジ
- 実行データはすべて `data/` 配下 (gitignore 済み)。このディレクトリを消せば初期状態に戻ります
