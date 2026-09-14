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
  - **vLLM / SGLang / FreeToken**: 各 `module/<name>` 内で `uv sync` (または `uv pip install -e .`) して作られる **`module/<name>/.venv` を自動検出**して使います。解決順は `明示設定 > module/<name>/.venv > GUI(llm-launcher)の .venv > launcher 自身`。検出時は各候補で実際の import を試し、成功した python を起動に使用します (バックエンド画面の「検出詳細」に試行結果を表示)
  - llama.cpp: `module/llama.cpp` をビルドすると `build/bin/llama-server` を自動検出。PATH 上の `llama-server` も対象。ビルド前でもバックエンド画面でパスを直接指定できます

```bash
cd module/llama.cpp && cmake -B build -DGGML_CUDA=ON && cmake --build build -j
```

## 使い方 (Web UI)

| タブ | できること |
| --- | --- |
| 🚀 起動 | 起動サーバーの選択画面。プロファイルカードをタップでスタート / 停止。バックエンド別フィルタ、稼働中サマリー付き |
| 🖥️ サーバー | 起動中 + 履歴の一覧。ステータス (起動中 / 稼働中 / 停止 / 異常終了)、PID・稼働時間・API URL、停止 / 再起動、ライブログ表示 (追従・自動スクロール) |
| 📋 プロファイル | **プロファイル管理画面**。一覧に検索 (名前/モデル/バックエンド/メモ)、並び替え (更新/名前/バックエンド/使用回数)、バックエンド別フィルタ (件数表示)。カードで有効パラメータ数・使用履歴 (起動回数/最終利用/異常終了数) を確認でき、起動/編集/複製/削除/JSON エクスポートを実行。**一括選択モード**でのまとめて削除/エクスポート、**JSON インポート** (他マシンへの移行・バックアップ) も可。編集画面はプルダウンでバックエンドを選ぶと起動パラメータが全項目リスト化・既定値入りで表示され、☑ で各フラグの送出 ON/OFF を制御 (vLLM 44項目 / SGLang 62項目 / llama.cpp 40項目 / FreeToken 45項目) |
| ⚙️ バックエンド | 各 submodule の状態 (取得済み / インストール検出)、起動コマンドテンプレートや python / バイナリパスの上書き設定、再検出 |

### プロファイル作成画面の流れ

1. バックエンドのプルダウンを選択 (vLLM / SGLang / llama.cpp / FreeToken)
2. 選んだ瞬間に、そのバックエンドの全起動パラメータがセクション別に並ぶ
   (モデル・提供設定 / 並列・分散 / スケジューリング・メモリ / 実行エンジン / サーバー・API / ツール呼び出し・LoRA / サンプリング / ログ)
3. 各項目には実プロダクトの**推奨値がプリフィル**され、さらに **☑「送信」チェックボックス**で各フラグを出す/出さないを切り替えられます。
   OFF にした項目はコマンドに一切含まれず、**モジュール側の自動判定 (モデルの学習済みコンテキスト長、dtype auto、生成設定など) を上書きしません**。
   特に自動導出されうる項目 (`--ctx-size` / `--max-model-len` / `--kv-cache-dtype` / `--mem-fraction-static` / attention バックエンド等) は既定 OFF+提案値表示、`--host`/`--port`/モデルなど必須項目は「必須」表示で常時 ON です
4. **モデル欄には HF Hub キャッシュのプルダウンが付く**: `~/.cache/huggingface/hub` (または `HF_HUB_CACHE` / `HF_HOME`) をスキャンし、ダウンロード済みのモデルをパス直書きなしで選択できます (llama.cpp なら snapshot 内の .gguf ファイルまで選択可。「再スキャン」で更新)
5. プレビューで最終コマンドを確認しながら編集。ON かつ空欄でない項目だけが `--flag 値` として付きます

### コマンド組み立ての仕組み

最終 argv = コマンドテンプレート (プロファイル上書き > バックエンド設定 > 既定)
+ リスト項目 (フォーム値) から生成した `--flag 群` (+ 古いプロファイルに残った追加引数)

- 各フラグの **型・選択肢・既定値は `src/llm_launcher/fields/<backend>.json` 正表が権威**で、これは `python -m llm_launcher.gen_fields --root .` で submodule の実ソース (vllm の tool_parsers/reasoning レジストリ・config dataclass の Literal、sglang の arg_groups/fields AST、freetoken の argparse、llama.cpp の common/arg.cpp) から生成されます。**submodule を `git submodule update` したら再生成してください**
- 生成器が追いきれない箇所 (llama.cpp の一部、sglang の動的 choices 等) は `backends.py` の補完表にソース照合済み値を持っています
- テンプレート内トークン: `{python}` `{llama_server}` `{module}` `{root}`、および `{model}` `{port}` などプロファイル変数
- 「追加引数」textarea は廃止しました。既存プロファイルに値が入っていた場合のみ、編集画面の「レガシー項目」として自動表示され保持されます
- 「コマンドのみモード」(高度な設定): ON にするとリスト項目のフラグ自動付与を止めます (カスタム起動の最終手段)
- 旧 v2 (1プロファイルに複数バックエンド) の `data/profiles.json` は、起動時に**バックエンドごとの別プロファイルへ自動分離**されます

## API (スクリプト等からも操作可能)

- `GET /api/state` — 全体状態 (バックエンド / プロファイル / セッション)
- `GET/POST/PUT/DELETE /api/profiles[/{id}]` — プロファイル CRUD。ボディは `{name, backend, values, on, extra_args, env, cwd, command, custom_only, note}`。`on` は各フラグの送信 ON/OFF (`{field_key: bool}`、**省略 = 旧来動作: 値があれば送信**)。旧 configs ボディは平坦化して互換受理
- `POST /api/sessions` `{profile_id, force?}` — 起動 (バックエンドはプロファイルに従う。ポート衝突は 409、`force` で回避可)
- `POST /api/sessions/{id}/stop` / `restart`、`GET /api/sessions/{id}/log?cursor=` — 停止 / 再起動 / ログ
- `GET /api/backends/{id}` ... バックエンド設定・再検出
- `GET /api/hf-models[?refresh=1]` — HF Hub キャッシュスキャン結果 (`{cache_dir, exists, models:[{repo_id, snapshot, revision, has_weights, gguf:[{name,size}]}]}`)
- `PUT /api/backends/{id}`、`POST /api/backends/{id}/detect` — バックエンド設定・再検出
- OpenAPI ドキュメント: `GET /api/docs`

## 動作仕様メモ

- バックエンドは `start_new_session` でプロセスグループ化して起動。停止はグループへ SIGTERM → 15 秒後に SIGKILL
- llm-launcher 本体を再起動しても生存中のサーバーは「引き継ぎ (detached)」として検出し、停止・ログ閲覧を継続できます
- `--port` / `-p` / `{port}` からポートを自動解釈し、`GET /health` を 3 秒間隔でプローブ → 緑の「稼働中」バッジ
- 実行データはすべて `data/` 配下 (gitignore 済み)。このディレクトリを消せば初期状態に戻ります
