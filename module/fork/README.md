# module/fork — フォーク専用クローン置き場

この配下の各フォルダは **フォークリポジトリのクローン専用** です。
llm-launcher 本体の git が追跡するのは **この `module/fork/` フォルダまで**
(目印の `.gitkeep` とこの README だけ) で、配下に clone した
フォークリポジトリ自体は一切追跡しません。submodule 化もしないため、
clone 後は自由にブランチ切替・コミット・push ができます。

## freetoken (suzuri-lollipop/FreeToken)

llm-launcher のルートディレクトリで:

```bash
git clone --branch suzuri/develop https://github.com/suzuri-lollipop/FreeToken module/fork/freetoken
# 推論環境の準備 (module/fork/freetoken/.venv/bin/python は WebUI の検出候補になる)
cd module/fork/freetoken && uv sync   # または pip install -e ./python など
```

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
