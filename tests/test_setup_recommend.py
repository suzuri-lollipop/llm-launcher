"""セットアップウィザード ③ の「環境適応の推奨」の回帰テスト。

背景 (2026-11-15): 色々な環境で使うため、稼働設定の「推奨」はコードに焼いた
固定値 (128K / fp8 / 4並列 / 32K) ではなく、この PC の GPU VRAM / compute
capability・CPU RAM・選択中のコンテキスト/KV/GPU分割/並列数から計算する。
GPU が検出できない環境では環境ベースの推奨を一切出さない (workload 推奨の
出力 32K / basalt vision dynamic だけ残す)。

見張る対象:
  1. sysinfo の GPU スナップショットが compute_cap を返す (fp8 対応判定の源)。
     古い nvidia-smi / [N/A] では None (不明 = 安全側)。
  2. setup.js の選択肢リストに固定「推奨」タグが焼かれていないこと。
  3. 推奨計算 (hwFromSystem / cfKvBytes / cfRecommend) の振る舞い:
     - KV 予算は VRAM 全量 (LLM 専用前提・一律の重み予約は計算外 — 2026-11-15 方針)
     - Strata はエンジン値が優先 (2026-11-09 の VRAM 拒否失敗の保護は engine 側)
     - 並列数 / コンテキスト / TP / カード選択 / RAM で推奨が動く
     - fp8 は compute capability 8.0+ と確認できたときだけ
     - basalt は KV 共有 (総量 = ctx、並列で割る) の予算則
     - Strata はエンジン値 (config parallel / 純正 rec_context) が優先
     - ctxList (描画済みの選択肢 = 家系ホワイトリスト) が渡されたらその範囲内で推奨
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from llm_launcher import sysinfo

ROOT = Path(__file__).resolve().parents[1]
SETUP_JS = (ROOT / "src" / "llm_launcher" / "static" / "setup.js").read_text("utf-8")

# 純粋関数ブロック (DOM / W 非依存) を node で単体テストする
PURE_RE = re.compile(r"const CF_TOK = .*?環境適応の推奨ここまで \*/", re.S)

NODE_DRIVER = """
{functions}
const items = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = items.map(it => {
  if (it.kind === "hw") return { name: it.name, hw: hwFromSystem(it.sys) };
  if (it.kind === "kvbytes") return { name: it.name, bytes: cfKvBytes(it.kv) };
  const hw = it.sys ? hwFromSystem(it.sys) : null;
  const r = cfRecommend(it.backend, hw, it.sel, it.engine, it.ctxList);
  const rec = {}, why = {};
  for (const k of ["ctx", "kv", "par", "out", "vis"]) {
    rec[k] = r[k] ? r[k].v : null;
    why[k] = r[k] ? r[k].why : null;
  }
  return { name: it.name, rec, why };
});
console.log(JSON.stringify(out));
"""


def _run(items: list[dict]) -> dict[str, dict]:
    m = PURE_RE.search(SETUP_JS)
    assert m, "推奨計算の純粋関数ブロックが setup.js に無い"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = NODE_DRIVER.replace("{functions}", m.group(0))
    out = subprocess.run([node, "-e", script], input=json.dumps(items, ensure_ascii=False),
                         capture_output=True, text=True, check=True)
    return {r["name"]: r for r in json.loads(out.stdout)}


def _gpu(vram_gib: float, cc: float | None = 8.9, index: str = "0",
         name: str = "GPU") -> dict:
    return {"index": index, "name": name, "mem_total_mib": vram_gib * 1024,
            "mem_used_mib": 0, "compute_cap": cc}


def _hw(gpus: list[dict], ram_gib: float | None = 128.0) -> dict:
    """/api/system の形 (hwFromSystem への入力)。RAM 無しは host.memory 欠落で表す。"""
    host = {"memory": {"total": int(ram_gib * 2**30)}} if ram_gib is not None else {}
    return {"gpus": gpus, "host": host}


def _sel(ctx="131072", kv="fp8", tp="1", par="4", gpu="auto") -> dict:
    return {"ctx": ctx, "kv": kv, "tp": tp, "par": par, "gpu": gpu}


# ---------------------------------------------------------------- sysinfo (compute_cap)

def test_parse_gpu_csv_reads_compute_cap():
    line = "0, NVIDIA RTX 5090, 3 %, 1024, 32607, 45, 120.5, 575.0, 12.0"
    g = sysinfo._parse_gpu_csv(line)[0]
    assert g["compute_cap"] == 12.0
    assert g["mem_total_mib"] == 32607


def test_parse_gpu_csv_old_nvidia_smi_without_compute_cap():
    # 8 列 (compute_cap 無し) でも壊れない — 不明は None
    line = "0, NVIDIA A100, 0 %, 0, 81920, 30, 60.0, 400.0"
    g = sysinfo._parse_gpu_csv(line)[0]
    assert g["compute_cap"] is None


def test_parse_gpu_csv_na_compute_cap_is_none():
    line = "0, NVIDIA X, 0 %, 0, 8192, 30, 60.0, 400.0, [N/A]"
    assert sysinfo._parse_gpu_csv(line)[0]["compute_cap"] is None


def test_system_api_passes_compute_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    async def fake_gpu():
        return {"gpus": [{"index": "0", "name": "X", "mem_total_mib": 24576,
                          "compute_cap": 8.9}], "error": ""}

    monkeypatch.setattr(sysinfo, "gpu_snapshot", fake_gpu)
    (tmp_path / "data").mkdir(parents=True)
    client = TestClient(app_mod.create_app(tmp_path))
    body = client.get("/api/system").json()
    assert body["gpus"][0]["compute_cap"] == 8.9
    assert body["host"]["memory"]["total"] > 0


# ---------------------------------------------------------------- 固定タグの廃止 (構造)

def test_no_static_recommendation_tags_baked_into_options():
    # 選択肢リストに「推奨」を焼かない (環境適応の推奨 cfRecommend が描画時に付ける)
    for baked in ('["131072", "128K", "推奨"]', '["4", "4", "推奨"]',
                  '["fp8", "fp8", "推奨"]', '["32768", "32K", "推奨"]',
                  '["int8", "int8", "推奨"]', '["dynamic", "dynamic", "推奨"]'):
        assert baked not in SETUP_JS, baked
    # 「既定」タグ (エンジン既定の事実) は残してよい
    assert '["8192", "8K", "既定"]' in SETUP_JS


def test_dynamic_recommendation_machinery_present():
    for marker in ("function hwFromSystem(", "function cfKvBytes(", "function cfRecommend(",
                   "function cfApplyRec(", "W.hw", "hwSummary(", "/api/system",
                   " · 推奨", "推奨の根拠"):
        assert marker in SETUP_JS, marker
    # GPU 不明環境では推奨を出さない旨の表示がある
    assert "GPU を検出できませんでした" in SETUP_JS


# ---------------------------------------------------------------- hwFromSystem / cfKvBytes

def test_hw_from_system_shapes_and_filters():
    got = _run([
        {"kind": "hw", "name": "null_gpus", "sys": {"gpus": None, "host": {}}},
        {"kind": "hw", "name": "no_gpus", "sys": {"gpus": [], "host": {}}},
        {"kind": "hw", "name": "zero_vram_filtered",
         "sys": {"gpus": [_gpu(0)], "host": {}}},
        {"kind": "hw", "name": "sorted_desc",
         "sys": {"gpus": [_gpu(8, index="0", name="small"),
                          _gpu(24, index="1", name="big")],
                 "host": {"memory": {"total": 64 * 2**30}}}},
        {"kind": "hw", "name": "cc_unknown_is_zero",
         "sys": {"gpus": [_gpu(24, cc=None)], "host": {}}},
    ])
    assert got["null_gpus"]["hw"] is None
    assert got["no_gpus"]["hw"] is None
    assert got["zero_vram_filtered"]["hw"] is None
    hw = got["sorted_desc"]["hw"]
    assert [g["name"] for g in hw["gpus"]] == ["big", "small"]   # VRAM 降順
    assert hw["ramGiB"] == 64
    assert got["cc_unknown_is_zero"]["hw"]["gpus"][0]["cc"] == 0


def test_kv_bytes_estimates():
    got = _run([{"kind": "kvbytes", "name": k, "kv": k}
                for k in ("fp8", "int8", "fp8_per_token_head", "bf16", "fp16",
                          "auto", "q4_0", "k8v4", "")])
    assert got["fp8"]["bytes"] == 48 * 1024
    assert got["int8"]["bytes"] == 48 * 1024
    assert got["fp8_per_token_head"]["bytes"] == 48 * 1024
    assert got["bf16"]["bytes"] == 96 * 1024
    assert got["fp16"]["bytes"] == 96 * 1024
    assert got["auto"]["bytes"] == 96 * 1024      # 不明は非圧縮 (保守的)
    assert got[""]["bytes"] == 96 * 1024
    assert got["q4_0"]["bytes"] == 24 * 1024
    assert got["k8v4"]["bytes"] == 36 * 1024


# ---------------------------------------------------------------- cfRecommend (環境適応)

def test_budget_is_full_vram_without_blind_reserve():
    """2026-11-15 方針: VRAM は LLM 専用なので全量を KV 予算の上限とする。

    一律の重み予約 (旧: 60%・最低12GiB) は計算外 — 重みの配置と起動時の実空き
    検査はエンジンの仕事。24GB・128K・fp8 (6GiB/slot) なら 4 並列が上限目安。
    """
    got = _run([{"kind": "rec", "name": "r", "backend": "freetoken-fork",
                 "sys": _hw([_gpu(24, cc=12.0)]),
                 "sel": _sel(ctx="131072", kv="fp8", par="4"), "engine": None}])
    assert got["r"]["rec"]["par"] == "4"          # 4 × 6GiB = 24GiB ≤ 24GB 全量
    assert "全量" in got["r"]["why"]["par"]
    assert "予約" not in got["r"]["why"]["par"]


def test_strata_engine_parallel_protects_the_verified_failure():
    """2026-11-09 の起動失敗 (VRAM 起因・実ログ検証済み) の保護はエンジン値側。

    失敗の実体: data/logs/s_6af118541aca.log (09:48:29, --parallel 4 / ctx 131072)
    と module/strata/strata-qwen36-ud-q4_k_xl.log のエンジン事前検査拒否:
    "not enough VRAM for 4 slots at a context of 131072
     (5.56 GiB each, 14.34 GiB free)" → exit 1。
    1 スロット = fp32 KV (45056 B/token) × 131072 + GDN state 0.07 GB = 5.56 GiB
    (上流 setup.py own_path_parallel_slot_gb の式と一致)。同日 32K×4 は成功 (exit 0)。
    全量予算の目安が 4 を出しても、Strata は config の parallel (=1) を優先する。
    """
    got = _run([{"kind": "rec", "name": "r", "backend": "strata",
                 "sys": _hw([_gpu(24, cc=12.0)]),
                 "sel": _sel(ctx="131072", kv="int8", par="4"),
                 "engine": {"parallel": 1, "recContext": 131072}}])
    assert got["r"]["rec"]["par"] == "1"
    assert "config の parallel" in got["r"]["why"]["par"]


def test_parallel_recommendation_scales_with_context():
    items = [{"kind": "rec", "name": c, "backend": "freetoken-fork",
              "sys": _hw([_gpu(24, cc=12.0)]),
              "sel": _sel(ctx=c, kv="fp8", par="1"), "engine": None}
             for c in ("32768", "131072", "262144")]
    got = _run(items)
    assert got["32768"]["rec"]["par"] == "8"      # 1.5GiB/slot × 8 = 12GiB ≤ 24GB
    assert got["131072"]["rec"]["par"] == "4"     # 6GiB/slot × 4 = 24GiB ≤ 24GB
    assert got["262144"]["rec"]["par"] == "2"     # 12GiB/slot × 2 = 24GiB ≤ 24GB


def test_warns_when_even_one_slot_exceeds_vram():
    got = _run([{"kind": "rec", "name": "r", "backend": "sglang",
                 "sys": _hw([_gpu(24, cc=12.0)]),
                 "sel": _sel(ctx="524288", kv="bf16", par="1"), "engine": None}])
    assert got["r"]["rec"]["par"] == "1"          # 48GiB/slot > 24GB
    assert "1スロットでも収まらない" in got["r"]["why"]["par"]


def test_parallel_recommendation_scales_with_vram_and_tp():
    items = [
        {"kind": "rec", "name": "h100", "backend": "sglang",
         "sys": _hw([_gpu(80, cc=9.0)]), "sel": _sel(par="4"), "engine": None},
        {"kind": "rec", "name": "tp1", "backend": "sglang",
         "sys": _hw([_gpu(24), _gpu(24, index="1")]), "sel": _sel(tp="1"), "engine": None},
        {"kind": "rec", "name": "tp2", "backend": "sglang",
         "sys": _hw([_gpu(24), _gpu(24, index="1")]), "sel": _sel(tp="2"), "engine": None},
    ]
    got = _run(items)
    assert got["h100"]["rec"]["par"] == "8"       # 6GiB/slot × 8 = 48GiB ≤ 80GB
    assert got["tp1"]["rec"]["par"] == "4"        # 1 枚 = 24GB 全量
    assert got["tp2"]["rec"]["par"] == "8"        # TP で予算が増えれば推奨も増える


def test_ram_caps_parallel_recommendation():
    got = _run([{"kind": "rec", "name": "r", "backend": "sglang",
                 "sys": _hw([_gpu(96, cc=9.0)], ram_gib=16),
                 "sel": _sel(ctx="32768", par="8"), "engine": None}])
    assert got["r"]["rec"]["par"] == "2"          # RAM 16GB → 4GiB/slot × 2 ≤ 8GB
    assert "RAM 16GB 上限" in got["r"]["why"]["par"]


def test_no_gpu_means_no_hardware_recommendations():
    got = _run([{"kind": "rec", "name": "r", "backend": "freetoken-fork",
                 "sys": None, "sel": _sel(), "engine": None},
                {"kind": "rec", "name": "b", "backend": "basalt",
                 "sys": None, "sel": _sel(), "engine": None}])
    for k in ("ctx", "kv", "par"):
        assert got["r"]["rec"][k] is None
    # workload 推奨 (環境非依存) だけ残る
    assert got["r"]["rec"]["out"] == "32768"
    assert got["b"]["rec"]["vis"] == "dynamic"


def test_fp8_recommendation_requires_compute_capability():
    items = [
        {"kind": "rec", "name": "sglang_turing", "backend": "sglang",
         "sys": _hw([_gpu(24, cc=7.5)]), "sel": _sel(), "engine": None},
        {"kind": "rec", "name": "sglang_ampere", "backend": "sglang",
         "sys": _hw([_gpu(24, cc=8.0)]), "sel": _sel(), "engine": None},
        {"kind": "rec", "name": "fork_turing", "backend": "freetoken-fork",
         "sys": _hw([_gpu(24, cc=7.5)]), "sel": _sel(), "engine": None},
        {"kind": "rec", "name": "fork_ampere", "backend": "freetoken-fork",
         "sys": _hw([_gpu(24, cc=8.9)]), "sel": _sel(), "engine": None},
        {"kind": "rec", "name": "sglang_cc_unknown", "backend": "sglang",
         "sys": _hw([_gpu(24, cc=None)]), "sel": _sel(), "engine": None},
    ]
    got = _run(items)
    assert got["sglang_turing"]["rec"]["kv"] == "bf16"
    assert got["sglang_ampere"]["rec"]["kv"] == "fp8"
    assert got["fork_turing"]["rec"]["kv"] == "int8"
    assert got["fork_ampere"]["rec"]["kv"] == "fp8"
    assert got["sglang_cc_unknown"]["rec"]["kv"] == "bf16"   # 不明 = 安全側


def test_kv_recommendation_relaxes_on_ample_vram():
    items = [{"kind": "rec", "name": b, "backend": b,
              "sys": _hw([_gpu(96, cc=9.0)]), "sel": _sel(), "engine": None}
             for b in ("sglang", "freetoken-fork", "strata", "basalt")]
    got = _run(items)
    assert got["sglang"]["rec"]["kv"] == "bf16"        # 品質優先
    assert got["freetoken-fork"]["rec"]["kv"] == "auto"  # エンジンにまかせる
    assert got["strata"]["rec"]["kv"] == "fp16"
    assert got["basalt"]["rec"]["kv"] == "fp16"


def test_basalt_kv_is_shared_across_slots():
    """basalt は KV 総量 = ctx (並列で割る) — 並列数を増やも ctx 推奨は変わらない。"""
    items = [{"kind": "rec", "name": f"par{p}", "backend": "basalt",
              "sys": _hw([_gpu(24, cc=12.0)]),
              "sel": _sel(ctx="131072", kv="int8", par=p), "engine": None}
             for p in ("1", "8")]
    got = _run(items)
    assert got["par1"]["rec"]["ctx"] == "524288"  # 24GiB (共有KV) ≤ 24GB 全量
    assert got["par8"]["rec"]["ctx"] == "524288"  # 並列数を増やしても変わらない
    assert got["par8"]["rec"]["par"] == "4"
    assert "共有" in got["par8"]["why"]["par"]


def test_strata_engine_values_override_estimates():
    """Strata はエンジン自身が決めた値 (config parallel / 純正 rec_context) が優先。"""
    got = _run([{"kind": "rec", "name": "r", "backend": "strata",
                 "sys": _hw([_gpu(96, cc=12.0)]),
                 "sel": _sel(ctx="262144", kv="int8", par="8"),
                 "engine": {"parallel": 2, "recContext": 204800}}])
    assert got["r"]["rec"]["par"] == "2"
    assert "config の parallel" in got["r"]["why"]["par"]
    # 200K は ③ の選択肢に無い → 選択肢内の最大 (128K) に丸めて推奨
    assert got["r"]["rec"]["ctx"] == "131072"
    assert "200K" in got["r"]["why"]["ctx"]


def test_strata_without_engine_values_falls_back_to_estimates():
    got = _run([{"kind": "rec", "name": "r", "backend": "strata",
                 "sys": _hw([_gpu(24, cc=12.0)]),
                 "sel": _sel(ctx="131072", kv="int8", par="4"),
                 "engine": {"parallel": None, "recContext": None}}])
    assert got["r"]["rec"]["par"] == "4"          # エンジン値が無ければ全量予算の目安


def test_output_recommendation_never_exceeds_context():
    got = _run([{"kind": "rec", "name": "small", "backend": "freetoken-fork",
                 "sys": _hw([_gpu(24)]), "sel": _sel(ctx="8192"), "engine": None},
                {"kind": "rec", "name": "normal", "backend": "freetoken-fork",
                 "sys": _hw([_gpu(24)]), "sel": _sel(ctx="131072"), "engine": None}])
    assert got["small"]["rec"]["out"] == "8192"
    assert got["normal"]["rec"]["out"] == "32768"


def test_context_options_include_512k_and_1m():
    """③ のコンテキスト長選択肢は 512K / 1M まで持つ (2026-11-15 追加)。
    strata/maya は CF_CTX に含まれたので旧来の concat を持たない (512K 重複禁止)。"""
    assert '["524288", "512K", ""]' in SETUP_JS
    assert '["1048576", "1M", ""]' in SETUP_JS
    assert "CF_CTX.concat" not in SETUP_JS
    # basalt の独自リストにも 512K / 1M が並ぶ
    m = re.search(r'"basalt": \{.*?ctx: \{.*?opts: \[(.*?)\], cur:', SETUP_JS, re.S)
    assert m and "524288" in m.group(1) and "1048576" in m.group(1)


def test_large_vram_recommends_512k_and_1m():
    """VRAM が大きければ 512K / 1M も推奨される (全量予算の上限目安)。"""
    got = _run([
        {"kind": "rec", "name": "1m", "backend": "sglang",
         "sys": _hw([_gpu(96, cc=9.0)]),
         "sel": _sel(ctx="1048576", kv="fp8", par="1"), "engine": None},
        {"kind": "rec", "name": "512k", "backend": "sglang",
         "sys": _hw([_gpu(96, cc=9.0)]),
         "sel": _sel(ctx="524288", kv="fp8", par="4"), "engine": None},
        {"kind": "rec", "name": "small", "backend": "sglang",
         "sys": _hw([_gpu(24, cc=12.0)]),
         "sel": _sel(ctx="1048576", kv="fp8", par="1"), "engine": None},
    ])
    assert got["1m"]["rec"]["ctx"] == "1048576"   # 48GiB/slot ≤ 96GB
    assert got["1m"]["rec"]["par"] == "2"         # 2 × 48GiB = 96GiB
    assert got["512k"]["rec"]["ctx"] == "524288"  # 4並列 × 24GiB = 96GiB
    assert got["512k"]["rec"]["par"] == "4"
    # 24GB では 1M は推奨されない (選択肢には在るがタグは付かない)
    assert got["small"]["rec"]["ctx"] != "1048576"


def test_strata_engine_rec_context_1m_label():
    """純正 rec_context が 1M のときも選択肢内に丸めず、1M ラベルで推奨できる。"""
    got = _run([{"kind": "rec", "name": "r", "backend": "strata",
                 "sys": _hw([_gpu(192, cc=12.0)]),
                 "sel": _sel(ctx="131072", kv="int8", par="1"),
                 "engine": {"parallel": None, "recContext": 1048576}}])
    assert got["r"]["rec"]["ctx"] == "1048576"
    assert "1M" in got["r"]["why"]["ctx"]


def test_recommendation_respects_rendered_ctx_whitelist():
    """描画済みの選択肢 (ctxList = 家系ホワイトリスト) が渡されたらその範囲内で
    推奨する。純正 rec_context (200K / 64K 等) は CF_CTX に無いが家系一覧に在る
    ときはそのまま推奨 — config 無し済みモデルのコンテキストを ③ が聞く経路
    (2026-11-17)。ctxList 無しは従来の CF_CTX 丸めを維持する。"""
    wl = [8192, 32768, 65536, 131072, 204800, 262144]   # own-path (Qwen3.6) 家系
    got = _run([
        {"kind": "rec", "name": "rec200k", "backend": "strata",
         "sys": _hw([_gpu(24, cc=12.0)]),
         "sel": _sel(ctx="131072", kv="int8", par="1"),
         "engine": {"parallel": None, "recContext": 204800}, "ctxList": wl},
        {"kind": "rec", "name": "rec64k", "backend": "strata",
         "sys": _hw([_gpu(24, cc=12.0)]),
         "sel": _sel(ctx="65536", kv="int8", par="1"),
         "engine": {"parallel": None, "recContext": 65536}, "ctxList": wl},
        {"kind": "rec", "name": "cap", "backend": "strata",
         "sys": _hw([_gpu(192, cc=12.0)]),
         "sel": _sel(ctx="131072", kv="int8", par="1"),
         "engine": None, "ctxList": wl},
        {"kind": "rec", "name": "default", "backend": "strata",
         "sys": _hw([_gpu(24, cc=12.0)]),
         "sel": _sel(ctx="131072", kv="int8", par="1"),
         "engine": {"parallel": None, "recContext": 204800}},
    ])
    assert got["rec200k"]["rec"]["ctx"] == "204800"   # 一覧に在る推奨は丸めない
    assert "200K" in got["rec200k"]["why"]["ctx"]
    assert got["rec64k"]["rec"]["ctx"] == "65536"
    assert got["cap"]["rec"]["ctx"] == "262144"       # VRAM が大きくても一覧を超えない
    assert got["default"]["rec"]["ctx"] == "131072"   # ctxList 無しは従来の丸め


def test_strata_basalt_card_selection_changes_budget():
    """カード選択 (GPU の使い方) に応じて予算が変わる: 2 枚共有 > 1 枚。"""
    hw = _hw([_gpu(24, index="0"), _gpu(24, index="1")])
    items = [{"kind": "rec", "name": "one", "backend": "strata", "sys": hw,
              "sel": _sel(kv="int8", par="1", gpu="0"), "engine": None},
             {"kind": "rec", "name": "both", "backend": "strata", "sys": hw,
              "sel": _sel(kv="int8", par="1", gpu="0,1"), "engine": None}]
    got = _run(items)
    assert got["one"]["rec"]["par"] == "4"        # 1 枚 24GB → 6GiB/slot × 4
    assert got["both"]["rec"]["par"] == "8"       # 2 枚 48GB → 6GiB/slot × 8