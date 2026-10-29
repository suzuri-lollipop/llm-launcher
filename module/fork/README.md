# module/fork — フォーク専用クローン置き場

この配下の各フォルダは **フォークリポジトリのクローン専用** です。
llm-launcher 本体の git が追跡するのは **この `module/fork/` フォルダまで**
(目印の `.gitkeep` とこの README だけ) で、配下に clone した
フォークリポジトリ自体は一切追跡しません。submodule 化もしないため、
clone 後は自由にブランチ切替・コミット・push ができます。

## freetoken (suzuri-lollipop/FreeToken)

**シェルを介さない方法**: llm-launcher を起動して WebUI の **セットアップタブ (📦)** で
「FreeToken (fork)」を選び、📦 インストール実行を押すだけです。
clone (`suzuri/develop` を `--depth 1`) → `.venv` 作成 → `pip install -e ".[accel]"` →
import 確認までランチャーが実行し、ログ・進捗・中断ボタンはこの画面に出ます
(バックエンドカードの「📦 インストール」ボタンからも飛べます)。

シェルでやる場合は llm-launcher のルートディレクトリで:

```bash
git clone --branch suzuri/develop https://github.com/suzuri-lollipop/FreeToken module/fork/freetoken
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

どちらの経路も同じインタープリタ・同じ env・同じ argv で起動します。従来どおり
`./setup.sh --model ID` のように引数を直接渡す起動 (JSON を読まない) もできます。

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
