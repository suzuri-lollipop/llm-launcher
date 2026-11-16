# 投機的デコード ボトルネック調査 (MTP / DFlash 2, TP2)

対象: `Freetoken(fork)/Qwen3.8-Flash-Next-NVFP4` / 2× RTX PRO 4000 Blackwell (24 GiB, sm_120), TP2, fp8 KV
モデル: 48層ハイブリッド (GDN + QSA) MoE、512エキスパート、NVFP4、**エキスパートはホストRAMにオフロードし decode は CPU 計算**
計測: greedy・512トークン生成の wall-clock(約1.5sのprefillを含む)、decodeログの `accept len`、`FREETOKEN_MOE_STATS` / `FREETOKEN_LATENCY`

## 1. 単一ストリーム decode スループット

| 構成 | wall tok/s | off比 | accept len | 受理率 (accept/(k+1)) |
|---|---|---|---|---|
| spec off | 41.1 | — | — | — |
| MTP k=1 | 44.1 | +7% | 1.79 | 89% (max 2) |
| **MTP k=2** | **50.2** | **+22%** | 2.47 | 82% (max 3) |
| MTP k=3 | 50.1 | +22% | 2.53 | 63% (max 4) |
| DFlash k=7 | 43.5 | +6% | ~2.33 | **29% (max 8)** |

- MTP は **k=2 で頭打ち**(k=3 と同等)。k を増やすと受理率が低下(89→63%)し、verify 行数(k+1)の増加コストが利得を打ち消す。
- DFlash は k=7 で7トークン起草するが平均2.33しか受理されず(受理率29%)、8行 verify の大半が無駄 → +6% に留まる。

## 2. ボトルネックの所在(実測)

### 支配的: CPU 側 MoE エキスパート計算(decode/verify)
- decode の MoE 統計(off): `cpu=0.89/layer, fetched=0.00, miss率8.9%, routes/layer=10.2`
  → **decode のエキスパートは GPU に fetch せず CPU で計算**(スロットキャッシュは prefill 専用)。
- 投機的 verify は **(k+1) 行 × 48 MoE層**を target で処理するため、CPU MoE 工作量が行数に比例して増える。
- 受理率がそれに見合わないため、増えた verify 計算の多くが捨てられる。これが両手法の利得を制限する根本原因。

### 副次: カーネル起動オーバーヘッド / TP 通信
- off のアクティブ窓のホスト位相: `fwd_issue 70% / sched 15% / recv 8% / drain 6%`
  → 小さなカーネルの起動(host launch)が大きな割合。GPU 計算待ち(drain)は小さい。
- TP の層ごと all-reduce はかかるが支配的ではない。

### prefill(並行負荷)は PCIe 律速
- MoE prefill 統計: `wall=1524ms, stall=1263ms(83%), gemm=24ms`
  → prefill はエキスパートの PCIe ストリーム待ち(stall)が83%。投機的デコードは prefill を高速化しない。

## 3. 並行負荷では投機は piped prefill に負ける

calibrate-multi の staggered 負荷(4リクエスト):
`piped-sf 51.3 > spec-mtp 38.3 > spec-dflash 34.0 tok/s`
- 並行では prefill(PCIe律速)が支配的で、spec は prefill を助けず、verify の CPU 負荷が並行 decode と競合するため不利。
- **投機的デコードは単一ストリーム最適化**であり、並行サービングでは piped prefill が勝る。

## 4. 手法別の結論

- **MTP**: 起草は1層( routed MoE + attention、CPU)。k=2 が最適(+22%)。ボトルネックは verify の CPU MoE 計算と受理率のトレードオフ。
- **DFlash 2**: 起草は5層の dense MLP(エキスパート無し)+ selector。k=7 のブロック起草は受理効率が低く(29%)、8行 verify のコストが利得を上回る。より高い受理率(優れたドラフト/小さいブロック)が必要。

## 5. 改善の方向性

1. **CPU MoE スループット向上**が両手法とベースラインのすべてを押し上げる(エキスパートの GPU 常駐増、CPU 計算の高速化)。
2. MTP は **k=2** を既定推奨。DFlash はブロック長 k=7 がこの target には深すぎる。
3. 並行サービングでは piped prefill を使う(spec は単一ストリーム向け)。

## 計測上の制約

- **spec decode 経路はホスト latency プローブの dump 地点を通過しない**(scheduler の drain ブロック外)。
  そのため spec のホスト位相内訳(recv/sched/fwd_issue/drain)は取得できず、decode ログの `accept len` と MoE 統計で代替した。
  spec 経路へのプローブ拡張は別途の diagnostics 改善課題。
- `gen throughput` ログは累積平均で窓ごとに大きく変動するため、wall-clock(512トークン)を主指標とした。
- sm_120 では dflash の ring/conv カーネルが fp32 参照と数値差异(既存 xfail)。スループット計測には影響しないが精度検証は別途。

---

## 6. torch.profiler によるカーネル内訳(実測・追補)

`FREETOKEN_PROFILE=<dir>` でスケジューラループに torch.profiler を仕込み(`engine/profiler.py`、env 未設定なら無効)、
`--cuda-graph-max-bs 0`(eager)で decode 窓を計測。`spec_draft` / `spec_verify_forward` の record_function で起草と検証を分離。
TP2 と TP1(NCCL 無し)の両方で計測。

### TP1(通信アーティファクト無しの純粋な計算内訳)

| バケット | off | MTP k=2 | DFlash k=7 |
|---|---|---|---|
| **moe_fetch**(エキスパート H2D / PCIe) | **93.9%** | **89.0%** | **85.3%** |
| moe_gemm(NVFP4/Marlin GEMM + W8A16 dense) | 1.5% | 8.1% | 11.3% |
| attention / conv_gdn / norm_act | 残り | 残り | 残り |

- 支配的カーネルは `fast_index_copy_multi`( pinned staging → GPU スロットキャッシュへのエキスパートコピー、`moe/offload_cache.py` / `locked_fetch.py`)。
  MTP 2958ms / DFlash 2871ms(各 768 calls)。**decode GPU 時間の 85〜94% が PCIe エキスパートストリーム**。
- 実計算は小さい: `_decode_nvfp4_marlin_kernel`(MoE GEMM)198ms、`_w8a16_main_kernel`(dense decode GEMM)155ms、attention/conv は 1% 未満。
- **verify ≫ draft(約22倍)**: MTP verify 3611ms / draft 162ms、DFlash verify 3588ms / draft 161ms(16ステップ合計)。
  起草自体は安く、target の verify(k+1 行)がコストの中心。DFlash は 8行 verify + 5層 dense ドラフトで GEMM がやや多い(11.3% vs 8.1%)。

### TP2 と計測アーティファクト(重要)

- TP2 では `ncclDevKernel_AllReduce` の self CUDA が **off 0.81 / MTP 0.05 / DFlash 1.28 ms/call** と run ごとに大きく変動。
  これは eager + profiler が TP ランクを非同期化し、AllReduce のスピン待ちを不均一に膨らませる**計測アーティファクト**。
  TP1 では通信無しでも moe_fetch が同じく支配的であることから、**真のボトルネックは PCIe エキスパートストリーム**であり、
  TP2 の tp_comm 割合は手法別の安定な指標ではない(DFlash の CPU selector がランク非同期を招きやすい可能性は残るが、本計測では分離できない)。

### 結論(プロファイラ確定)

1. **GPU 側 decode のボトルネックは MoE エキスパートの PCIe ストリーム(`fast_index_copy_multi`)で 85〜94%**。attention や GEMM ではない。
2. 投機的 verify は (k+1) 行ぶんのエキスパートをストリームするため、このコストが行数倍になる。受理率が利得を制限(§1)。
3. 起草コストは verify の約 1/22 で小さい。MTP・DFlash とも「verify のエキスパートストリーム」が支配的。
4. 改善の最有力は **エキスパートの GPU 常駐増 / PCIe ストリーム削減**(ベースラインと両手法すべてを押し上げる)。

### 仕込んだ道具(再利用可)

- `python/freetoken/engine/profiler.py`: `FREETOKEN_PROFILE=<dir>` / `FREETOKEN_PROFILE_WAIT` / `FREETOKEN_PROFILE_ACTIVE`。
  CUDA/CPU カーネル表・chrome trace・バケット内訳を pid 別に出力。未設定時は無効(ループは `is None` チェックのみ)。
- `spec/runner.py`: `spec_draft` / `spec_verify_forward` の record_function(未プロファイル時は no-op)。
- 計測時は `--cuda-graph-max-bs 0` で eager 化(カーネルを可視化)。TP2 の nccl はアーティファクトに注意、純計算は TP1 で見る。

---

## 7. エキスパート常駐増の効果(追加実測)

「GPU 常駐エキスパートを増やせば PCIe ストリームが減って速くなるか」を検証。
ベースラインは既に `--moe-cache-auto`(VRAM 優先でエキスパートキャッシュを自動最大化、KV は `--kv-reserve-tokens` 下限)。
KV 容量を削ってエキスパートキャッシュを増やす構成と、逆にキャッシュを絞る構成を比較(512トークン greedy、TP2、fp8 KV)。

| 構成 | エキスパートスロット | KV pages | miss/layer | decode tok/s |
|---|---|---|---|---|
| baseline(524K KV + auto) | 8955 | 8192 | 0.88 (8.8%) | **41.2** |
| more experts(131K KV + auto) | 10933 (+22%) | 2050 | 0.83 (8.3%) | **42.0 (+2%)** |
| fewer experts(`--moe-cache-size 2000`) | 2000 (−78%) | 8192 | 3.68 (36.8%) | **16.7 (−60%)** |

### 結論:自動キャッシュは既にほぼ最適。増やしても僅か、減らすと壊滅

- **増やす方向は収穫逓減**: スロット +22%(8955→10933)で miss は 8.8%→8.3% にしか下がらず、スループットは **+2%**。
  ベースラインの miss が既に低く(8.8%)、キャッシュは「ニー(膝)」付近にあるため。
- **減らす方向は壊滅的**: スロット −78%(→2000)で miss が 36.8% に急増し、スループット **−60%**(41.2→16.7)。
  miss の CPU 計算 + PCIe フェッチコストが支配的になる。
- したがって §6 の `fast_index_copy_multi`(GPU 時間の 85〜94%)の大部分は **miss 由来の PCIe フェッチではなく、
  常駐(hit)エキスパートを計算バッファへステージングする毎ステップ固定コスト**。これはキャッシュ増では減らない
  (hit ルート ≈10 expert/layer × 48層 はキャッシュサイズに依らず一定)。miss はその上乗せの 8.8% 分。

### 含意

- `--moe-cache-auto` の既定が既に well-tuned。**手動でキャッシュを絞るのは厳禁**(−60%)。KV を削っての常駐増も +2% 程度で、KV 容量を犠牲にする価値は薄い。
- 真のレバーは「ステージングコピー自体の削減」(バンク内 in-place 計算、より高速なティアへの常駐など)であって、キャッシュサイズ調整ではない。
- 投機的デコードの利得制限(§1)も同じ構造:verify 行数倍のステージング + miss 上乗せを、受理率が償却しきれない。