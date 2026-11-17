"""セットアップ画面の順序統一 (FreeToken 基準) の回帰テスト。

プロファイル編集 (起動パラメータ) のセクション順は以前「フィールドリストでの
初出順」だったため、項目を追加するたびにバックエンドごとに順序がずれていた:
  - llama.cpp の exec (実行エンジン・最適化) が server より後に出ていた
  - sglang の --admin-api-key が section 省略で model セクションに混入していた
  - basalt の --max-context / --kv が section 省略 + parallel の後ろの宣言だった
SECTION_ORDER (FreeToken のセットアップ基準) で全バックエンドを統一する。

セットアップウィザード (/setup) の ③ 稼働設定も同じ基準で揃える:
  - 質問順は ctx → out → kv → tp → par → vis の固定順 (定義側の並びに依存しない)
  - freetoken (upstream) の RUNCFG 欠落で ③④ が丸ごとスキップされ、
    プロファイルが作られない状態を fork と同じ導線に直す
"""
from __future__ import annotations

import re
from pathlib import Path

from llm_launcher.backends import (BACKENDS, SECTIONS_JA, SECTION_ORDER,
                                   load_table, sections_order)
from llm_launcher.installer import INSTALL_SPECS

ROOT = Path(__file__).resolve().parents[1]
WIZARD_JS = (ROOT / "src" / "llm_launcher" / "static" / "setup.js").read_text("utf-8")


def _runcfg_block(bid: str) -> str:
    """RUNCFG の 1 バックエンド分の定義ブロックを取り出す。"""
    m = re.search(r'"%s": \{(.*?)\} \},' % re.escape(bid), WIZARD_JS, re.S)
    assert m, f"RUNCFG に {bid} が無い"
    return m.group(1)


# ---------------------------------------------------------------- セクション順 (編集画面)

def test_freetoken_defines_the_baseline_order():
    assert sections_order(BACKENDS["freetoken"]) == [
        "model", "parallel", "sched", "cache", "exec", "server", "tools", "logs"]
    # fork は基底と同じ並び (追加項目は各セクションの末尾に入る)
    assert sections_order(BACKENDS["freetoken-fork"]) == sections_order(BACKENDS["freetoken"])


def test_every_backend_follows_the_canonical_order():
    """全バックエンドのセクション順は SECTION_ORDER の部分列 ( FreeToken 基準)。
    かつフィールドが持つセクションはすべて表示順に含まれる (取りこぼし無し)。"""
    for bid, b in BACKENDS.items():
        order = sections_order(b)
        present = {f.get("section", "model") for f in b["fields"]}
        assert order == [s for s in SECTION_ORDER if s in present], bid
        assert set(order) == present, bid


def test_llamacpp_exec_before_server():
    order = sections_order(BACKENDS["llamacpp"])
    assert order.index("exec") < order.index("server")


def test_sglang_admin_api_key_is_a_server_field():
    f = next(f for f in BACKENDS["sglang"]["fields"] if f["key"] == "admin_api_key")
    assert f["section"] == "server"


def test_basalt_context_and_kv_are_explicit_model_fields():
    fields = BACKENDS["basalt"]["fields"]
    for key in ("max_context", "kv"):
        f = next(f for f in fields if f["key"] == key)
        assert f["section"] == "model", key
    # 宣言順も表示順と同じ (model ブロックが parallel より前にまとまる)
    keys = [f["key"] for f in fields]
    assert keys.index("max_context") < keys.index("gpu")
    assert keys.index("kv") < keys.index("gpu")


def test_sections_ja_covers_the_canonical_order():
    assert set(SECTIONS_JA) == set(SECTION_ORDER)


# ---------------------------------------------------------------- ウィザード (③ 稼働設定)

def test_wizard_question_order_is_fixed():
    """描画 / 補足 / 保存の 3 ループとも同じ固定順 (FreeToken 基準の
    ctx → out → kv → tp → par → vis)。定義オブジェクトの並び順には依存しない。"""
    assert WIZARD_JS.count('for (const id of ["ctx", "out", "kv", "tp", "par", "vis"])') == 3


def test_wizard_covers_every_installable_backend():
    """UI インストールできるバックエンドは全て ③ 稼働設定の質問セットを持つ。
    欠落すると ③④ がスキップされ、プロファイルも作られない。"""
    for bid in INSTALL_SPECS:
        _runcfg_block(bid)


def test_wizard_asks_gpu_split_where_the_engine_has_it():
    """FreeToken 基準の GPU 分割質問は、エンジンが複数 GPU に対応するバックエンド全てで出す。
    strata は layer split (config の gpu)、basalt はカード選択 (gpu。tensor parallel は
    価格評価のみで未実装 = 質問は TP 数ではなくカード選択)、maya/sglang/vllm は TP。"""
    for bid, key in (("strata", "gpu_split"), ("basalt", "gpu"), ("maya", "tp"),
                     ("sglang", "tp_size"), ("vllm", "tensor_parallel_size")):
        assert f'key: "{key}"' in _runcfg_block(bid), bid
    # strata / basalt の GPU 質問は 2 枚以上のときだけ描画する
    assert 'sec.flag === "gpu" && (W.cfgArgs.gpus || []).length < 2' in WIZARD_JS
    assert 'sec.gpusFrom === "system" && (W.sysGpus || []).length < 2' in WIZARD_JS


def test_llamacpp_editor_exposes_engine_multi_gpu_flags():
    """common/arg.cpp に実在する multi-GPU ノブを編集画面が出すこと
    (2026-11-11 のコード監査: 正表にはあるのに UI に無かった)。"""
    fields = {f["key"]: f for f in BACKENDS["llamacpp"]["fields"]}
    for key in ("device", "split_mode", "tensor_split", "main_gpu"):
        assert key in fields, key
        assert fields[key]["section"] == "parallel"
        assert fields[key]["default_on"] is False   # エンジン既定を上書きしない
    assert fields["split_mode"]["type"] == "select"
    assert "tensor" in fields["split_mode"]["options"]


def test_wizard_freetoken_matches_the_fork_flow():
    up, fork = _runcfg_block("freetoken"), _runcfg_block("freetoken-fork")
    # fork と同じ導線: 起動 JSON 再生成 + モデル解決
    assert "regen: true" in up and "model: true" in up
    # 質問は fork の部分集合で、相対順は同じ (ctx → out → tp → par)
    up_keys = re.findall(r'key: "(\w+)"', up)
    fork_keys = re.findall(r'key: "(\w+)"', fork)
    assert up_keys == [k for k in fork_keys if k in up_keys]
    assert up_keys == ["num_tokens", "max_output_tokens", "tp_size", "max_running_requests"]
    # upstream に実在するフラグだけを聞く (--tp-size は alias なので別名も込みで判定)
    tbl = load_table("freetoken")
    known = set(tbl)
    for spec in tbl.values():
        known.update((spec or {}).get("aliases", []))
    flags = {f["key"]: f["flag"] for f in BACKENDS["freetoken"]["fields"]}
    for k in up_keys:
        flag = flags[k]
        assert flag in known, flag
    # --kv-cache-dtype は fork 独自 (upstream 正表に無い) ので聞かない
    assert "kv_cache_dtype" not in up_keys
    assert "--kv-cache-dtype" not in tbl
    assert "kv_cache_dtype" in fork_keys


# ---------------------------------------------------------------- ダウンロード択の取得済みガード

def test_wizard_download_list_greys_downloaded_models():
    """ダウンロードするモデルの一覧で取得済みは選択不可 (グレー)。
    済みモデルは「ダウンロード済みのモデルを使う」側で選ぶ — 再ダウンロードの
    誤起動防止 (2026-11-16 報告: 取得済みがダウンロード択で選べてしまった)。"""
    assert "const haveRepo = r => mdLocal.some(x => x.repo_id === r);" in WIZARD_JS
    assert '${got ? " disabled" : ""}' in WIZARD_JS
    assert " — ダウンロード済み" in WIZARD_JS
    # 一覧の先頭が取得済みでも最初の未取得を選ぶ
    assert "const firstAvail = ms.findIndex(m => !haveRepo(m.repo_id));" in WIZARD_JS
    # 開始ボタンはグレー選択を弾く (表パネル / 汎用一覧の両方)
    assert "!!(sizeOpt && sizeOpt.disabled)" in WIZARD_JS
    assert "!!(selOpt && selOpt.disabled)" in WIZARD_JS
    # 表パネル (Strata / Maya) の既存グレーも維持
    assert '${mdStFull(m) || unpub ? " disabled" : ""}' in WIZARD_JS
