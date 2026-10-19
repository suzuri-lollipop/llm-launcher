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
```

upstream submodule の `module/strata` (Niko1221/Strata) とは別のクローンです。
`deepseek/` サブフォルダに、project-maya (`module/maya/tools/maya_quant`) を参考にした
**DeepSeek-V4.1-Flash-1017 の 3bit (IQ3_S) 量子化 + 実行ツールキット** が入っています
(GGUF 量子化、NVMe/RAM/VRAM の階層ストリーミング実行、FP8 原本との品質比較)。
使い方と実測値は `module/fork/strata/deepseek/README.md`。
モデル本体は `/media/nvme2/models/DeepSeek-V4.1-Flash-1017{,-IQ3}` に置きます。

## strata-fork (suzuri-lollipop/Strata の並列チューニング worktree)

`module/fork/strata` clone (deepseek 系ブランチ使用中でも可) から **git worktree** で
`module/fork/strata-fork` に展開します。WebUI のバックエンド一覧に **Strata (fork)** として表示され、
`--parallel / --batch-groups / --batch-mtp / --adapt-async / --adapt-every / --piped-concurrent / --yield-chunks`
(2026-10 の並列チューニング) を含む 25 項目を出します。

```bash
cd module/fork/strata
git fetch ../../strata suzuri/parallel-perf:suzuri/parallel-perf   # submodule 側からブランチ取得 (push 不要)
git worktree add ../strata-fork suzuri/parallel-perf
ln -s ../../strata/.venv ../strata-fork/.venv                      # 検出/起動用 python (module/strata の setup 済み venv)
```

エンジンバイナリと `strata-<model>.json` は module/strata のものを `--config` で共有できます
(config の "exe" が絶対パスのため)。fork 専用エンジンが必要なときは strata-fork 内で ./setup.sh を実行。
