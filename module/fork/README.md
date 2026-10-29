# module/fork — フォーク専用クローン置き場

この配下の各フォルダは **フォークリポジトリのクローン専用** です。
llm-launcher 本体の git が追跡するのは **この `module/fork/` フォルダまで**
(目印の `.gitkeep` とこの README だけ) で、配下に clone した
フォークリポジトリ自体は一切追跡しません。submodule 化もしないため、
clone 後は自由にブランチ切替・コミット・push ができます。

## freetoken (suzuri-lollipop/FreeToken)

**シェルを介さない方法**: llm-launcher を起動して WebUI の **セットアップタブ (📦)** で
「FreeToken (fork)」を選び、ブランチ (**安定版 `suzuri/product`** / **開発版 `suzuri/develop`**、
既定は安定版) を選んで 📦 インストール実行を押すだけです。
clone (`--depth 1 --branch <選んだブランチ>`) → `.venv` 作成 → `uv pip install -e ".[accel]"` →
import 確認までランチャーが実行し、ログ・進捗・中断ボタンはこの画面に出ます
(バックエンドカードの「📦 インストール」ボタンからも飛べます)。

**すでに clone 済みのとき**は、選んだブランチが現在のブランチと違えば
`git fetch origin <branch>` → `git checkout` を install より前に実行します (venv / インストールは
そのあと)。安全弁として、追跡ファイルに未コミットの変更があるときは切替を失敗にしてお知らせし、
同じ名前のローカルブランチが既にあるときは `merge --ff-only` で進む分だけ進めます
(ローカルの独自コミットは壊しません)。ブランチを変えずに入れ直すだけなら、
現在のブランチ (画面に出る `current_ref`) を選べば clone は触られません。

**インストールの続き (② モデルダウンロード / ③ キャリブレーション)**: 同じセットアップ画面で
repo id を入れるダウンロードは `module/fork/Freetoken-data/models/<org>--<name>` に置き、完了後に
`freetoken-launch.json` の `--model-path` / `model_name` を自動で指し替えます。さらに下の
「③ キャリブレーション」カードでこのフォークのプロファイルを選んで計走まで続けます
(`auto` 選定は ~3-4 時間、実行中は対象サーバーを停止して GPU を独占)。① の
「完了後にキャリブレーション」をオンにすると、インストール成功後に同じバックエンドの
プロファイルがあれば自動で続けます (該当 0 件 / 複数のときは走らせずにお知らせ)。
プロファイルが未作成のときは作成案内だけを出すので、まず 📋 プロファイルでモデルパスを指定してください。

シェルでやる場合は llm-launcher のルートディレクトリで:

```bash
git clone --branch suzuri/product https://github.com/suzuri-lollipop/FreeToken module/fork/freetoken
# (開発版を使うなら --branch suzuri/develop)
cd module/fork/freetoken && ./setup.sh          # .venv 作成 + install + serve 起動 (Strata 同様)
# ./setup.sh --no-start なら install のみ。uv があれば uv pip、無ければ venv の pip を使う
```

**起動 JSON (Strata の `strata-<model>.json` 方式)**: setup.sh と UI インストールが
クローン先に `freetoken-launch.json` のテンプレート (`{python, argv, cwd, env, port}`、
既存ファイルは絶対に上書きしない) を作成します。`--model-path` を編集すれば

- **ランチャー不经由の standalone**: `./setup.sh` (引数なし) がこの JSON を正に起動
  (`python -m freetoken.serve_spec freetoken-launch.json` と同内容)
- **ランチャー起動**: プロファイル編集の「起動 JSON パス」にこのパスを入れると、
  エンジン引数・python・env の正がこの JSON になる (キャリブレーション勝者の env も
  ここ経由で起動実プロセスへ効く)。フォームはポートと API キーの上書きだけ担う

どちらの経路も同じインタープリタ・同じ env・同じ argv で起動します。

**Strata 方式の一発起動 (setup 完結)**: `./setup.sh --model <repo id>` が
Hugging Face から `module/fork/Freetoken-data/models/<org>--<name>` へ
resume 対応でダウンロードし (Strata-data / Maya-data と同じ「フォルダ隣接」方式、
`--data-dir` / `FREETOKEN_DATA_DIR` で変更可)、実パスを `freetoken-launch.json` に
書き込み、`run-<model>.sh` 相当の **`run.sh`** を clone 先に生成します。
以降は `./run.sh` だけで standalone 起動できます (`--model <ローカルフォルダ>` なら
ダウンロードせず JSON にパスを記入するだけ)。JSON を読ませず引数を直接 engine へ
渡す起動 (`./setup.sh --port 8080 ...`) も従来どおり使えます。

WebUI のセットアップタブ (モデルダウンロード) でも同じ置き場を選べます: 保存先で
「FreeToken (fork) のデータフォルダ」を選ぶと `module/fork/Freetoken-data/models/<org>--<name>`
へ落とし (setup.sh と同一規則・同一 env)、完了後 `freetoken-launch.json` の
`--model-path` / `model_name` だけを更新し、モデル欄のプルダウン (📦 表示) にも出ます。

WebUI のバックエンド一覧には **FreeToken (fork)** として表示され、
upstream submodule の `module/freetoken` (FreeToken) とは別々に起動できます。
python のパスはバックエンド設定から明示指定も可能です
(未指定時は `module/fork/freetoken/.venv` → GUI `.venv` → launcher の順に自動検出)。

起動引数の正表 (`src/llm_launcher/fields/freetoken-fork.json`) は
fork を更新した後 `python -m llm_launcher.gen_fields` で再生成できます。

## strata (suzuri-lollipop/Strata)

llm-launcher のルートディレクトリで:

```bash
git clone https://github.com/suzuri-lollipop/Strata module/fork/strata
# (clone 先を指定しないと git は既定でリポジトリ名の module/fork/Strata を作りますが、
#  ランチャーは大小文字を無視して認識するのでどちらでも動きます)
ln -s ../../strata/.venv module/fork/strata/.venv   # 検出/起動用 python (module/strata の setup 済み venv を共有)
```

WebUI のバックエンド一覧に **Strata (fork)** として表示されます (clone が無い環境では非表示)。
現在の `suzuri/deepseek-v41` ブランチには並列チューニング (`suzuri/parallel-perf` の内容:
`--parallel / --batch-groups / --batch-mtp / --adapt-async / --adapt-every / --piped-concurrent /
--yield-chunks` と layer split + arena での `--adapt-async` 対応) が cherry-pick 済みで、
`deepseek/` ツールキット (DeepSeek-V4.1 の量子化・階層ストリーミング実行、将来このフォークへ合流予定)
と同じ clone に同居します。専用バックエンドだった **DeepSeek-V4.1 3-bit (fork) はランチャーから削除済み**
(ツールキット自体は `deepseek/README.md` のとおり CLI で使えます)。

エンジンバイナリと `strata-<model>.json` は module/strata のものを `--config` で共有できます
(config の "exe" が絶対パスのため)。fork 専用エンジンが必要なときはこの clone で ./setup.sh を実行。

起動引数の正表 (`src/llm_launcher/fields/strata-fork.json`) は fork を更新した後
`python -m llm_launcher.gen_fields --only strata-fork` で再生成できます。
