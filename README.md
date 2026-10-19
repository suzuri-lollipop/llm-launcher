# llm-launcher

vLLM / SGLang / llama.cpp / FreeToken / Strata / Project Maya を **サブモジュールとして `module/` 配下に登録** し、
Web UI から **起動プロファイル管理・ワンタップ起動・ログ確認・停止** まで行える管理アプリです。
スマホ (同一 LAN) からもブラウザで操作できます。

## 構成

```
llm-launcher/
├── module/               # バックエンド群 (git submodule)
│   ├── vllm/             # https://github.com/vllm-project/vllm
│   ├── sglang/           # https://github.com/sgl-project/sglang
│   ├── llama.cpp/        # https://github.com/ggml-org/llama.cpp
│   ├── freetoken/        # https://github.com/FlashML-org/FreeToken
│   ├── strata/           # https://github.com/Niko1221/Strata
│   └── maya/             # https://github.com/mw00/project-maya
├── module/fork/          # フォーク専用クローン置き場 (フォルダのみ追跡・内容は追跡外)
│   └── freetoken/        # 例: https://github.com/suzuri-lollipop/FreeToken を clone
├── src/llm_launcher/     # 管理アプリ本体 (FastAPI + 静的 SPA)
│   ├── app.py            # REST API
│   ├── process.py        # プロセス起動/監視/停止 (ログ・health probe・detached 引き継ぎ)
│   ├── backends.py       # バックエンド定義・引数テンプレート
│   ├── bench.py          # 組み込みベンチマークランナー (並列度別スループット/TTFT/遅延分布)
│   ├── sysinfo.py        # ホーム画面用ホスト/GPU統計 (/proc・nvidia-smi)
│   └── static/index.html # Web UI (PC / スマホ両対応)
└── data/                 # 実行データ (gitignore 済み): プロファイル / 起動履歴 / ベンチ履歴 / ログ
```

## セットアップ

```bash
# 1. 依存インストール + サブモジュール取得 (clone した直後)
uv sync
git submodule update --init --depth 1

# 1b. (フォークを使う場合) module/fork/ 配下に clone
#    ※ module/fork/ はフォルダのみ追跡。配下のリポジトリは追跡外 (submodule 化もしない)
git clone --branch suzuri/develop https://github.com/suzuri-lollipop/FreeToken module/fork/freetoken

# 2. 管理アプリを起動 (既定で 0.0.0.0:8036 で待ち受け / 認証 ON)
uv run llm-launcher
```

起動時に表示される `http://<LAN-IP>:8036` にスマホ / PC のブラウザからアクセスしてください。
**初めてのアクセス時は管理者アカウント作成画面**が出るので、ユーザー名とパスワード (8 文字以上) を登録するとそのままログインします (→ [認証・ユーザー管理](#認証ユーザー管理))。

- `--host` / `--port` オプション、または `LLM_LAUNCHER_HOST` / `LLM_LAUNCHER_PORT` 環境変数で変更可能
- 認証を切って従来どおり誰でもアクセスできる状態で起動する場合: `uv run llm-launcher --no-auth` (または `LLM_LAUNCHER_AUTH=0`) ※ LAN 内・開発用
- 常駐させたい場合は systemctl に登録できます (sudo 不要): `scripts/systemd-install.sh` → [systemctl に登録](#systemctl-に登録-常駐運用)
- (任意・推奨) 同じ PC で重い CLI 等を動かす場合は OOM 対策ヘルパーを入れておくと、モデルが OOM killer の一番の犠牲者になりにくくなります: `sudo scripts/oom-protect-install.sh` → [OOM 対策](#oom-対策-重い-cli-と同居させる環境向け)
- バックエンド自体のインストールは各自の環境に依存します:
  - **vLLM / SGLang / FreeToken**: 各 `module/<name>` 内で `uv sync` (または `uv pip install -e .`) して作られる **`module/<name>/.venv` を自動検出**して使います。解決順は `明示設定 > module/<name>/.venv > GUI(llm-launcher)の .venv > launcher 自身`。検出時は各候補で実際の import を試し、成功した python を起動に使用します (バックエンド画面の「検出詳細」に試行結果を表示)
  - llama.cpp: `module/llama.cpp` をビルドすると `build/bin/llama-server` を自動検出。PATH 上の `llama-server` も対象。ビルド前でもバックエンド画面でパスを直接指定できます
  - **Strata**: サーバー本体は `module/strata/serve/server.py` (Python)。初回に `./setup.sh` を実行すると `.venv`・エンジン・モデル設定 JSON (`strata-<model>.json`) が生成されます。検出は `module/strata` を cwd にして `serve.server` を**実 import** して行うため、setup 前 (依存の jinja2 が無い) は未検出になります。モデル・コンテキスト長などの本体設定は CLI ではなく `strata-<model>.json` の `args` に入るため、GUI からは `--config` でその JSON を指すだけで起動できます
   - **Strata 並列推論チューニング** (2026-10 追加): `strata-<model>.json` に `"parallel": N` (同時リクエスト数 = バッチスロット) のほか、`"batch_groups"` (`1` = 全スロットを 1 ウィンドウで直列デコード。`"auto"`/`N` = layer split のステージ毎のパイプライン群)、`"batch_mtp": true` (各スロットで MTP ドラフト検証)、`"adapt_async": true` (適応 VRAM エキスパート tier のラウンドを非ブロック化。layer split + arena 構成対応はローカルエンジンパッチ E1 による)、`"adapt_every": N` (tier ラウンド間隔) を書けます。`args` に同等オプションが既にある場合はそちらが優先されます。**実測による使い分け** (2x RTX PRO 4000 Blackwell + Qwen3.8-Flash-Next NVFP4、詳細は `.scratch/strata-par/RESULTS.md` と `module/strata/docs/BATCHING.md`): **4-8 人の並列運用 → pipelined 既定 + env `"STRATA_PIPED_CONCURRENT": "1"`** (8並列 125.1 tok/s 総 / 直列比 +37%、巨大プロンプト読み込み中のストリーム停止 39.5s→1.2s。現行の `strata-nvfp4-nvfp4-nvidia.json` / `strata-nvidia-fp4-local.json` はこれ、`"parallel": 8`)。**1-2 人主体のエージェント運用 → 直列系** (`strata-nvfp4-nvfp4-nvidia-serial.json`: `"batch_groups": 1` + `"batch_mtp"` + `"adapt_async"` + env `STRATA_BATCH_YIELD_CHUNKS=1`。ソロ +5-10%・2並列 +6%・長い読み込み中の短い質問 TTFT 30s→6.5s。4並列以上では pipelined に負ける)。プロファイルを複製して `--config` を差し替えると切り替えできます。`STRATA_ADMIT_SHORT_FIRST=2048` (短いプロンプト優先 admission) は**サーバー側 env** なので、使う場合はプロファイルの env に設定してください (config の "env" はエンジンにしか届きません)
  - **Project Maya**: 入口は `module/maya/maya.sh` → `maya.py` (セットアップ+起動ラッパ、Strata 系の GLM-5.3-Flash 専用エンジン)。初回に `./maya.sh` を実行すると `.venv`・エンジンビルド・モデル (~100GB) DL を対話的に済ませます。セットアップ済みなら GUI からの起動は対話なしで最新 (または `--config` で選んだ) `maya-<model>.json` を立てます。検出は `module/maya` を cwd にして `serve.server` を実 import (依存は遅延 import なので GUI .venv でも通るが、setup 済みなら `module/maya/.venv` が最優先で選ばれる)。モデル・コンテキスト長などの本体設定は `maya-<model>.json` の `args` / `env` に入るため、GUI からは起動フラグ (config 選択 / GPU / サーバー) だけを出す

```bash
cd module/llama.cpp && cmake -B build -DGGML_CUDA=ON && cmake --build build -j
cd module/strata && ./setup.sh          # .venv + エンジン + strata-<model>.json を生成 (対話的)
cd module/maya && ./maya.sh             # .venv + エンジン + モデル DL + ダッシュボード起動 (対話的・初回のみ)
```

## systemctl に登録 (常駐運用)

`uv run llm-launcher` を**ユーザーサービス**として登録できます (sudo 不要)。

```bash
scripts/systemd-install.sh              # インストール + 自動起動登録 + 起動
scripts/systemd-install.sh --port 8100  # 待ち受けホスト / ポートを変える場合
scripts/systemd-install.sh --uninstall  # 停止 + 登録解除 + ユニット削除
```

- 管理コマンド: `systemctl --user status llm-launcher` / `{start,stop,restart}`、ログ: `journalctl --user -u llm-launcher -f`
- OS 起動時 (ログイン前) に立ち上げるには Linger が必要です。未設定なら `sudo loginctl enable-linger $USER` を一度だけ実行
- ユーザーマネージャーの PATH に `~/.local/bin` は含まれないため、ユニットには uv の絶対パス (`~/.local/bin/uv`) を埋め込みます
- **`systemctl --user` で restart / stop しても稼働中のバックエンドサーバーは巻き添えになりません** (ユニットは `KillMode=process`。本体再起動後は生存サーバーを「引き継ぎ」として再検出)。サーバーごと停止するには先に UI から停止してください
- `uv run` は起動時に `.venv` を `uv.lock` と同期するため、`git pull` 後の restart で依存更新も反映されます
- OOM 対策モード 1 (sudoers ヘルパー) はこのサービスからも有効です (起動ログに `oom-guard:` が出ます)。モード 2 (`sudo scripts/oom-protect-install.sh --systemd`) はシステム側に**同名**の別ユニット (`/etc/systemd/system/llm-launcher.service`, venv python 実行・ポート 8088) を作るため、併用すると二重起動になります。切り替えるときは `systemctl --user disable --now llm-launcher` してから

## 使い方 (Web UI)

| タブ | できること |
| --- | --- |
| 🏠 ホーム | **現在の状況を確認できるダッシュボード (既定タブ)**。異常終了 / 未検出バックエンドの警告、タイル状サマリー (稼働サーバー / プロファイル数 / バックエンド検出 / GPU平均使用率 / 異常終了履歴 / ホスト稼働時間)、ホスト情報、リソース使用率バー (メモリ / スワップ / ディスク / CPU負荷)、GPU別メーター (使用率・VRAM・温度・消費電力)、稼働中サーバーのクイック停止、バックエンド状態、最近のセッション (行タップで該当セッションのタブへ移動)。`/api/system` を 4 秒間隔で自動更新 |
| 🚀 起動 | 起動サーバーの選択画面。プロファイルカードをタップでスタート / 停止。バックエンド別フィルタ、稼働中サマリー付き |
| 🖥️ 稼働中 | **起動中のサーバーだけを扱う操作画面**。ステータス (稼働中 / 起動中 = API 応答待ち / 停止中)、PID・稼働時間・API URL・OOM 保護状態、停止 / 再起動、ライブログ表示 (追従・自動スクロール)、プレイグラウンド / ベンチへの導線。終了した起動は表示されません。Strata 系は停止時にエンジンの子プロセスがピン留め arena (数十 GiB) を解放し切るまで「停止中」のまま待ち (上限 `LLM_LAUNCHER_STOP_DRAIN_S`、既定 240s)、解放中のエンジンが残る間の再起動はメモリガードが拒否します (OS 過コミット凍結の防止)。サイドバーに起動中の件数バッジを表示 |
| 🕘 セッション履歴 | **終了した起動 (履歴) を探す画面**。行一覧で開始→終了時刻・稼働時間・終了コード・PID・バックエンドを表示し、検索 (プロファイル / バックエンド) と状態フィルタ (すべて / 異常終了 / 終了 / 切断記録、件数付き) で絞り込み。行タップでログ展開 (追従・DL・コピー)、`▶ 再実行` で元プロファイルを起動、削除が可能。件数は上限なく `さらに表示` で30件ずつ展開でき、サイドバーに異常終了件数のバッジを表示 |
| 🧪 プレイグラウンド | **起動中のサーバーと直接チャットできる Playground**。サーバー / モデル選択 (`/v1/models`)、システムプロンプト、temperature / top_p / max_tokens、SSE ストリーム描画 (思考内容 = reasoning の折りたたみ表示、所要時間 / first token 遅延 (TTFT) / **生成速度 tok/s をストリーミング中にリアルタイム表示** / トークン数 (prompt→completion) の統計)、Enter 送信 / Shift+Enter 改行 / Stop ボタンで中断、会話はブラウザ (localStorage・サーバー毎) に自動保存。コードフェンスのシンタックス風表示とコピーボタン付き。稼働中サーバーのカード / ホーム / 稼働中画面の 🧪 ボタンからも入室可 |
| 📊 ベンチマーク | **作成済みプロファイルのサーバーを直接計測する組み込みベンチ**。対象プロファイルを選択 (未起動なら自動起動、完了後の自動停止も可) し、並列度リスト (例 `1,8,32`) を各 1 ステージとして逐次実行。リクエスト数 / prompt 語数 / max_tokens / temperature / エンドポイント (chat・completions) / stream / warmup を設定でき、プロンプトはリクエスト毎に重複しないランダム文章で prefix cache を抑制。ステージごとに **出力 tok/s・合計 tok/s・req/s・TTFT (avg/p99)・遅延分布 (p50/p99)・decode tok/s** を算出し、実行中はライブ進行バー (フェーズ・累積 tok/s)。履歴は `data/bench.json` に最大 50 件保存され、結果テーブル展開 / 設定の再読込 / JSON エクスポート / 削除 / 中断が可能。トークン数は API の `usage` 優先、非対応サーバーは文字数推定 (結果テーブルに注記)。同時実走は 1 件 (GPU 計測の互干渉防止)。追加依存なしの標準ライブラリのみで動作 |
| 📋 プロファイル | **プロファイル管理画面**。一覧に検索 (名前/モデル/バックエンド/メモ)、並び替え (更新/名前/バックエンド/使用回数)、バックエンド別フィルタ (件数表示)。カードで有効パラメータ数・使用履歴 (起動回数/最終利用/異常終了数) を確認でき、起動/編集/複製/削除/JSON エクスポートを実行。**一括選択モード**でのまとめて削除/エクスポート、**JSON インポート** (他マシンへの移行・バックアップ) も可。編集画面はプルダウンでバックエンドを選ぶと起動パラメータが全項目リスト化・既定値入りで表示され、☑ で各フラグの送出 ON/OFF を制御 (vLLM 44項目 / SGLang 62項目 / llama.cpp 40項目 / FreeToken 45項目 / FreeToken (fork) 56項目 / Strata 25項目 / Strata (fork) 25項目 / Project Maya 11項目。fork は MTP 投機的デコード `--speculative` やカーネル最適化、Strata 並列チューニング (セットアップ節の「Strata 並列推論チューニング」参照) など fork 独自項目を含む。**フォーク系バックエンドは `module/fork/` に clone が無い環境では UI に表示されません**。clone ディレクトリ名は大小文字を無視して認識します (git clone 既定の `module/fork/Strata` でも可)。旧 DeepSeek-V4.1 3-bit (fork) バックエンドは削除済み — deepseek/ ツールキットは Strata フォークへ合流予定で、CLI からは引き続き利用可) |
| 👥 ユーザー | **管理者のみ表示できるユーザー管理画面**。ロール権限カード (メンテナーに開示する機能の ON/OFF、簡易画面に表示するプロファイルの一覧と連絡先メッセージ) を筆頭に、ユーザー一覧 (ロール / 有効状態 / 最終ログイン)、新規作成・編集・有効化 / 無効化・削除、パスワードリセット、API キーの発行 / 再生成 / 失効 (平文は発行時の 1 回だけ表示)。認証を無効化して起動している場合は利用不可の案内を表示 |
| ⚙️ バックエンド | 各 submodule の状態 (取得済み / インストール検出)、起動コマンドテンプレートや python / バイナリパスの上書き設定、再検出 |
| 👤 簡易画面 `/simple` | **ロール `user` のユーザー向けの別ページ** (`http://<host>:8036/simple`)。開始・停止だけを押せます。詳細は [簡易画面](#簡易画面-simple) |

### プロファイル作成画面の流れ

1. バックエンドのプルダウンを選択 (vLLM / SGLang / llama.cpp / FreeToken / Strata / Project Maya)
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

- 各フラグの **型・選択肢・既定値は `src/llm_launcher/fields/<backend>.json` 正表が権威**で、これは `python -m llm_launcher.gen_fields --root .` で submodule の実ソース (vllm の tool_parsers/reasoning レジストリ・config dataclass の Literal、sglang の arg_groups/fields AST、freetoken の argparse、llama.cpp の common/arg.cpp、strata の serve/server.py の argparse、Project Maya の maya.py の argparse) から生成されます。**submodule を `git submodule update` したら再生成してください**。`--only freetoken-fork` を付けると 1 バックエンド分だけ再生成できます (fork 版正表は `module/fork/freetoken` のソースから生成。未 clone 時は基底 FreeToken 表を流用。strata 表は `module/strata` が未クローンのとき skip、maya 表は `module/maya` が未クローンのとき skip)。Strata はサーバー起動フラグ (--config / --engine / --host / --port など) のみが CLI にあり、モデル・コンテキスト長などの本体設定は `strata-<model>.json` の `args` 側に入るため正表には含めません。Project Maya も同様に、モデル本体設定は `maya-<model>.json` の `args` / `env` 側に入り、正表には起動ラッパ maya.py のフラグ (セットアップ系も含む。GUI に出すのは起動系のみ) が入ります
- 生成器が追いきれない箇所 (llama.cpp の一部、sglang の動的 choices 等) は `backends.py` の補完表にソース照合済み値を持っています。CLI に無い GUI 独自項目 (例: FreeToken (fork) の「カーネル最適化」= 環境変数マッピング) は、正表に `--` 始まり以外の key (例: `kernel_optimization`) で記録しておけば再生成後も自動で引き継がれます
- テンプレート内トークン: `{python}` `{llama_server}` `{module}` `{root}`、および `{model}` `{port}` などプロファイル変数
- 「追加引数」textarea は廃止しました。既存プロファイルに値が入っていた場合のみ、編集画面の「レガシー項目」として自動表示され保持されます
- 「コマンドのみモード」(高度な設定): ON にするとリスト項目のフラグ自動付与を止めます (カスタム起動の最終手段)
- 旧 v2 (1プロファイルに複数バックエンド) の `data/profiles.json` は、起動時に**バックエンドごとの別プロファイルへ自動分離**されます

## 簡易画面 `/simple`

`http://<host>:8036/simple` は **ロール `user` のユーザー専用** (ログインするとその画面しか開けない) のページです。管理画面 (`/`) 側でロールを `user` にしたアカウントは、ログイン成功時に自動的に `/simple` へリダイレクトされます。

- 表示されるのは **プロファイル編集の「簡易画面に表示」(`simple_visible`) を ON にしたものだけ** (オプトイン)。一覧のカードは「▶ 起動する / ■ 停止する」の 1 ボタン + 小さな状態バッジ (稼働中 / 起動中 / 停止中) だけ。**カードをタップ (または「▶ 起動する」) すると全画面の大きな表示に切り替わり、他のカードも一覧もすべて隠れます** (音楽プレイヤー風: 中央に `稼働中` / `停止中` の大文字 + 稼働中だけ跳ねるイコライザ、下に「■ 停止する / ▶ 起動する」と「← 一覧に戻る」だけ)。開いたサーバーはセッション中そのまま、**1 台だけ稼働中のときは読み込み時に自動で全画面へ**展開します («← 一覧に戻る» で解除)。設定値・ログ・ホスト情報・API キーは一切渡しません
- **ログイン / ログアウト / セッション切れは管理画面と同じ `/` のログイン画面**を使います (`/simple` 側に別の認証フォームは置きません)。ログアウトやセッション切れでは `/` へ戻り、理由 (「ログアウトしました」等) を 1 回表示します。ロール `user` なので `/` でログインし直すと再び `/simple` へ戻ります
- **異常は全画面警告**になりますが、対象は **そのユーザーが自分で起こした (`/simple` の「▶ 起動する」/ 管理画面の起動・再起動) 現在のインスタンス**が `failed` (起動失敗) / `lost` (通信不能) になったときだけです。ベンチマークの自動起動や他のユーザー (管理者) の起動、起動者情報を知らない以前の履歴では警告せず、**そのサーバーが今まさに稼働中なら、過去の異常終了を警告しに行くこともありません** (自分の起動分だけなので「もう一度起動する」が正しい案内になります)。警告中は画面いっぱいの赤い警告 + 警告音 (30 秒ごとに再通知) + **管理者が設定した連絡先メッセージ** (例: とまらないときは 内線 1234 へ) を表示、「今は様子を見るだけ」で 90 秒だけ閉じます
- **メモリ逼迫 (`/api/oom` の state=crit) だけは例外**で、サーバーの起動者に関係なくマシン全体の異常として常に警告します
- 5 秒間隔の自動更新。API は `GET /api/simple` / `POST /api/simple/start` / `POST /api/simple/stop` の 3 本だけで、`simple_visible` OFF のプロファイルは start/stop しても `404`
- **縦が足りない環境でもスクロール不要**: 全画面表示 (プレイヤー / 警告) は円盤・状態文字・ボタン高・余白をビューポート高さに連動 (`clamp` + `dvh`) させて自動で縮めるので、横画面スマホ (例: 640×360) でも「■ 停止する / ← 一覧に戻る」まで 1 画面に収まります。一覧側も高さ 560px 以下ではカードとボタンが詰まります (デスクトップ / 縦長スマホの見た目は従来どおり)


## 認証・ユーザー管理

既定で認証が **ON** です。`/api/*` はログイン前だと `401 {"detail":"ログインが必要です","code":"unauthorized"}` を返し、トップ画面 (ログイン / 管理者作成フォーム) だけが開けます。

- **初回セットアップ**: ユーザーが 0 人のとき、最初の 1 人だけ `POST /api/auth/setup` で管理者を作成できます (作成 = 自動ログイン)。以降の setup は `403`。表示名は未指定ならユーザー名になり、画面右上は `@ユーザー名` + ロールバッジの別表示です (表示名の変更は ユーザー管理画面から)。CI 等で先回りして作りたい場合は `LLM_LAUNCHER_ADMIN_USER` / `LLM_LAUNCHER_ADMIN_PASSWORD` を設定して起動 (ユーザーが存在する場合は無視され、上書きもしません)
- **セッション**: ログイン成功時に `llm_session` Cookie (ランダム 32 バイトのトークン、`HttpOnly` / `SameSite=Lax` / `Path=/`、HTTPS アクセス時は `Secure`) を発行。既定の有効期限は 12 時間、ログイン画面の「ログイン状態を保持する」で 30 日。セッションは `data/sessions.json` に保存するのでサーバー再起動後も維持されます。ログアウト / パスワード変更 / アカウント無効化 / 削除で失効
- **パスワード**: PBKDF2-HMAC-SHA256 (200,000 反復) + 16 バイトソルト。`data/users.json` には `pbkdf2_sha256$反復回数$ソルト$ハッシュ` の形で保存し、平文は一切書きません。比較は `hmac.compare_digest`、ログイン失敗が 5 回続くと 30 秒ロック (429)、失敗時はユーザー名の存在を匂わせるメッセージを出しません
- **ロール** (3 段階):
  - `admin` (管理者): 全機能 + ユーザー管理 + ロール権限の設定
  - `maintainer` (メンテナー): 起動 / 停止 / 再起動、`/api/state` 閲覧は常に可。それ以外は管理者が ON/OFF を切り替える機能キーで、既定は **ON** = ダッシュボード / 起動ログ / セッション履歴 / プレイグラウンド / 設定値の閲覧、**OFF** = ベンチマーク / プロファイル編集 / バックエンド設定 / OOM 対策の設定 / ユーザー管理。変更は `GET/PUT /api/roles/maintainer` (ユーザー管理画面のロール権限カード)
  - `user` (ユーザー): **`/simple` 画面だけ**。通常 API は 403 (`/api/simple*` と自分のパスワード変更のみ可)。ログイン時に `/simple` へリダイレクト
  - 最後の管理者が自分自身を削除 / 無効化 / 降格するのは 409 で拒否。ロール権限は `data/settings.json` の `roles` / `simple` に保存し、`GET /api/roles` で一覧できます
- **権限の効き方**: サーバー側は `request.state.perms` で API を絞り、`dashboard` OFF ならホスト詳細を、`config.read` OFF なら設定値・コマンド・ログパスをレスポンスから落します。Web UI は `GET /api/auth/status` が返す `role` / `perms` / `simple_only` でタブとボタンを隠すので、**見えないもの＝叩っても 403** が揃います
- **API キー** (任意): 各ユーザーに 1 本。`Authorization: Bearer llm_<40桁の16進>` を送ると Cookie なしで API を叩けます。ロール制限はそのまま適用。平文は保存せず SHA-256 ハッシュのみ記録、**発行 / 再生成したときだけ 1 回表示**されます。再生成ですぐに旧キーが無効化、失効 (DELETE) も可能
- **認証を切る**: `uv run llm-launcher --no-auth` または `LLM_LAUNCHER_AUTH=0`。全 API が認証なしで使えます (開発・単機検証用)。この状態ではユーザー管理 API は `400` を返し、Web UI のユーザータブは案内表示になります

保存先 (いずれも `data/` 配下、`0600` で作成。`data/` は `.gitignore` 済み):

| ファイル | 内容 |
| --- | --- |
| `data/users.json` | ユーザー (パスワードハッシュ / API キーの SHA-256 ハッシュ / ロール / 有効フラグ) |
| `data/sessions.json` | ログインセッション (セッショントークン・有効期限・ユーザー ID) |

## API (スクリプト等からも操作可能)

- `GET /api/auth/status` — 認証設定とログイン状態 `{auth_enabled, needs_setup, authenticated, is_admin, user, role, perms, simple_only}` (認証 API 自体は常に認証不要)。`perms` は capability 名の真偽値、`simple_only` はロール `user` (通常 API が全部 403) のとき true
- `POST /api/auth/setup` `{username, password}` — 初回のみ管理者作成 (201 + 自動ログイン)
- `POST /api/auth/login` `{username, password, remember?}` / `POST /api/auth/logout` — ログイン / ログアウト (ロール `user` は管理画面側が `simple_only` を見て `/simple` へリダイレクト。`/simple` 側のログイン / ログアウト / セッション切れはこの `/` のログイン画面を使う)
- `POST /api/auth/password` `{current_password, password}` — 自分のパスワード変更 (全セッション失効 + `relogin: true` を返す)
- `GET/POST/PUT/DELETE /api/users[/{id}]` — ユーザー管理 (管理者のみ)。POST `{username, password, display_name?, role?, enabled?}` (`role` = `admin | maintainer | user`)、PUT は部分更新、`POST /api/users/{id}/password` `{password}` でリセット、`POST /api/users/{id}/apikey` で発行 / 再生成 (平文は 1 回のみ返す)、`DELETE /api/users/{id}/apikey` で失効
- `GET /api/roles` — (管理者) capability 一覧と現在のロール設定 `{caps, roles, simple, simple_profiles}`
- `GET/PUT /api/roles/maintainer` — (管理者) メンテナーの開示機能。PUT `{perms: {<cap>: bool}}` (管理者自身とユーザーロールの権限は変更不可)
- `PUT /api/roles/simple` — (管理者) 簡易画面に出す連絡先メッセージ。PUT `{contact}`
- `GET /api/simple` — (ロール user) 表示対象プロファイルの状態と警告 `{hostname, contact, server_time, servers:[{profile_id, profile_name, sid, state, ready, since}], alerts:[{id, kind, sid, profile_id, profile_name, detail, at}]}`。`state` = `running | starting | stopping | stopped`、`kind` = `failed | lost | memory`。サーバー起因の `alerts` は **そのユーザーが自分で起こした (`/api/simple/start` / 管理画面の起動・再起動) 現在のインスタンス** (failed / lost) だけを対象に出します (ベンチマークの自動起動や他人の起動、過去の履歴、いま稼働中のインスタンスは対象外)。`memory` は起動者に依存しないライブ判定なので常に出ます
- `POST /api/simple/start` `{profile_id}` / `POST /api/simple/stop` `{session_id}` — (ロール user) 表示対象だけの起動 / 停止 (それ以外は 404)

- `GET /api/state` — 全体状態 (バックエンド / プロファイル / セッション)
- `GET /api/system` — ホスト / GPU スナップショット (ホーム画面用)。`/proc` と `nvidia-smi` を追加依存なしで読取。返り値 `{host:{cpu_count,load,uptime,memory,disk,...}, gpus:[{index,name,util,mem_used_mib,mem_total_mib,temp,power,power_limit}], error}`。非 NVIDIA 環境では `gpus:null`。GPU は 4 秒 TTL + single-flight キャッシュ
- `GET/POST/PUT/DELETE /api/profiles[/{id}]` — プロファイル CRUD。ボディは `{name, backend, values, on, extra_args, env, cwd, command, custom_only, note}`。`on` は各フラグの送信 ON/OFF (`{field_key: bool}`、**省略 = 旧来動作: 値があれば送信**)。旧 configs ボディは平坦化して互換受理
- `POST /api/sessions` `{profile_id, force?}` — 起動 (バックエンドはプロファイルに従う。ポート衝突は 409、`force` で回避可)。空きメモリが予約値を大きく下回ると **メモリガードが 409 で起動を拒否** (同じく `force: true` で強制起動)。起動したユーザーと経路を `started_by` / `started_by_name` / `started_via` (`ui` = 管理画面、`simple` = 簡易画面、`bench` = ベンチマークの自動起動) としてセッションに記録し、簡易画面の警告判定に使う
- `POST /api/sessions/{id}/stop` / `restart` (`{force?}`)、`GET /api/sessions/{id}/log?cursor=` — 停止 / 再起動 / ログ (再起動時もメモリガードが働くため `{force:true}` を送れます)
- `GET /api/oom` — OOM 対策の状態スナップショット `{enabled, score_adj, min_available_gb, guard_mode, reapply, swift_stop, can_lower, helper, self_applied, self_via, self_error, self_score, self_unit, oomd, oom_kills, state, available, reserve, used_pct, verdict, level, install_hint}` (`level` = `ok|warn|off`、`state` = `ok|warn|crit`)
- `POST /api/oom` `{設定の一括/部分更新}` — 保存してその場で再適用 (launcher 自身 + 起動中セッションのツリー)。未指定キーは現状維持、範囲外はクリップ。返り値は `GET /api/oom` と同じ形
- `GET /api/backends/{id}` ... バックエンド設定・再検出
- `GET /api/hf-models[?refresh=1]` — HF Hub キャッシュスキャン結果 (`{cache_dir, exists, models:[{repo_id, snapshot, revision, has_weights, gguf:[{name,size}]}]}`)
- `GET /api/playground/{session_id}/models` — そのセッションの OpenAI 互換 API (`/v1/models`) への透過プロキシ (プレイグラウンド用。ブラウザからの CORS を回避)
- `POST /api/playground/{session_id}/chat` — `/v1/chat/completions` への透過プロキシ。ボディ `{messages, model?, stream?, temperature?, top_p?, max_tokens?}`。`stream:true` (既定) は SSE をそのままストリーム返却。このとき launcher は `stream_options:{include_usage:true}` を付与し、ストリームの最終チャンクに usage (トークン数) を含めさせてプレイグラウンドの生成速度 (tok/s) 統計を正確に表示する。`stream` フラグを無視するサーバーへのフォールバックあり (SSE→JSON 集約 / JSON→SSE 組替)
- `GET /api/benchmarks` — ベンチ履歴一覧 `{runs:[...], active:id|null}`。`POST /api/benchmarks` `{profile_id, model?, endpoint?, stream?, concurrency:[..], num_prompts, input_len, output_len, temperature?, warmup, request_timeout, ready_timeout, auto_start, stop_after}` で実行開始 (未起動プロファイルは auto_start で自動起動。同時 1 件)、`GET /api/benchmarks/{id}` (進捗 `progress` とステージ結果 `stages` 付き)、`POST /api/benchmarks/{id}/cancel`、`DELETE /api/benchmarks/{id}`。ステージ結果は `{concurrency, ok, fail, rps, out_tps, total_tps, ttft:{avg,p50,p90,p99,...}, latency:{...}, decode_tps:{...}, errors:[...]}`
- `PUT /api/backends/{id}`、`POST /api/backends/{id}/detect` — バックエンド設定・再検出
- OpenAPI ドキュメント: `GET /api/docs`

## OOM 対策 (重い CLI と同居させる環境向け)

モデルインスタンスは RSS が最大のことが多く、空きが数 GB を切った瞬間にカーネルの OOM killer の一番の犠牲者になります。llm-launcher は次の多重で守ります (設定は `data/settings.json` の `oom`、UI は「バックエンド設定 → 🛡 OOM 対策」)。

- **起動前メモリガード** — `MemAvailable` が予約 (`min_available_gb`、既定 8 GB) を大きく下回ると起動を拒否 (`guard_mode: "block"` が既定)。`"warn"` は警告のみ、`"off"` は無効。拒否時は API が 409 を返し、`force: true` で強制起動できます
- **oom_score_adj の引き下げ** — 既定 -700 を起動ツリー (モデル本体 + 後から湧く scheduler / worker) と launcher 自身に適用し、kill 順を後回しにします
- **定期再適用** — バックエンドが起動後に増やした subprocess へも数分おきに再適用します (`reapply`)
- **逼迫時の safety stop** — `swift_stop: true` にすると、critical な逼迫で launcher 側が最も新しく起動したセッションを安全停止します (カーネルに kill されるより軽い障害で復旧できます)
- **OOM kill の推定** — SIGKILL / exit 137 と cgroup `memory.events` の `oom_kill` 増分から「OOM kill の可能性」をセッションカードとログに表示します

**権限についての現実**: `oom_score_adj` を**下げる**には `CAP_SYS_RESOURCE` が必要です。一般ユーザーでは EACCES で弾かれるため、UI と `GET /api/oom` は「権限不足」を隠さず表示し、以下の導入を案内します。

```bash
sudo scripts/oom-protect-install.sh              # 1) 専用 sudo ヘルパー (推奨・launcher 再起動のみで有効)
sudo scripts/oom-protect-install.sh --systemd    # 2) システムサービス化 (OOMScoreAdjust=-1000 + MemoryHigh)
sudo scripts/oom-protect-install.sh --swap 32G   # 3) スワップが無い環境だけで実メモリの受け皿を追加
sudo scripts/oom-protect-install.sh --uninstall  # 削除
```

- 1) は `/usr/local/lib/llm-launcher/oom-protect` にヘルパーを置き、sudoers で「**引数なしのそのパスだけ**」を NOPASSWD で許可します。stdin で `pid` と `score` の 2 行だけを受け取り、`/proc/<pid>/oom_score_adj` をその PID と子孫にのみ書き込みます (親方向 — sshd やユーザーセッション — には書きません)。`--user` で対象ユーザーを指定できます
- 2) はユニットに `MemoryHigh` (実メモリの 85%) / `MemoryMax` (92%) を書くため、超過分は kill ではなく throttle され、同居するプロセスごと巻き添えになりにくくなります。ユーザーセッション (`systemd-run --user`) では `OOMScoreAdjust` が無視されるので、必ず**システム**側に入れてください
- 適用状況はセッションカード (🛡 保護中 / 未適用とその理由 / 逼迫 / OOM 推定) とホームの通知に表示され、セッションログには `[oom]` 行として記録されます


## 動作仕様メモ

- バックエンドは `start_new_session` でプロセスグループ化して起動。停止はグループへ SIGTERM → 15 秒後に SIGKILL
- llm-launcher 本体を再起動しても生存中のサーバーは「引き継ぎ (detached)」として検出し、停止・ログ閲覧を継続できます
- `--port` / `-p` / `{port}` からポートを自動解釈し、`GET /health` を 3 秒間隔でプローブ → 緑の「稼働中」バッジ
- 実行データはすべて `data/` 配下 (gitignore 済み)。このディレクトリを消せば初期状態に戻ります

## ライセンス

llm-launcher 本体は **MIT License** ([LICENSE](LICENSE)) です。

`module/` 配下のサブモジュールは本リポジトリの一部ではなく、各自の upstream ライセンスに従います
(vLLM: Apache-2.0 / SGLang: MIT / llama.cpp: MIT / FreeToken: 各リポジトリ参照 / Strata: MIT / Project Maya: MIT)。
本アプリはこれらをサブプロセスとして起動するだけでコードをリンクしないため、ライセンス上の制約は及びません。
