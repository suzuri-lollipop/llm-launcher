"""Regression tests for selective auto-refresh (操作中のオブジェクトを巻き込まない).

2026-11-24 の complaints: ポーリング由来の再描画が「中身が変わったか」ではなく
「ポーリングが来たか」で innerHTML を組み直していたため、

* 入力途中 / 未保存のフォーム (バックエンドのコマンド上書き・OOM 設定・デコード速度の
  リセット時刻) が数秒ごとに消えたりサーバー値へ巻き戻ったりする
* 開いているドロップダウン (coding のプロファイル選択は無条件で毎ポーリング作り直し)
  が閉じて選択が飛ぶ
* 見ているもの (テキスト選択中のログ / 結果表 / イコライザアニメーション) が
  解除・初期化される
* メーターの width トランジションが毎ポーリング初期化されて棒が跳ねる

のが「既定」になっていた。修正は core.js の共通ガード層に集約する:

* ``uiBusyIn(el)`` — 箱が入力フォーカス / IME 変換 / マウス押下 / テキスト選択 /
  有限アニメーション進行中なら true (無限ループの spin / pulse は数えない)
* ``setHtml(el, html, key, redo)`` — キャッシュ一致なら触らない / 操作中・未保存編集
  (``formDirty``) があれば ``deferRender`` で後回し / それ以外だけ snap→write→restore
  →``stampForm``。初回 (箱が空) は後回しにしない
* ``setOptions(sel, html, key, redo, pick)`` — 選択肢はフォーカスがある間組み直さない
* ``meterPatch`` / ``_meterValues`` / ``patchUptime`` — 構造が変わらない数値は
  in-place で書き換え、トランジションと DOM ノードを生かす
* ``patchChips`` — フィルタは顔ぶれが変わらなければ選択クラスだけ切り替える

この repo にブラウザテストハーネスは無いので、test_bench_scroll_keep.py と同じく
ソースレベルで契約を固定する。将来の描画関数がガードを忘れたらここで気づく。
"""
import re
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static"


def _frontend_src() -> str:
    parts = [(BASE / "index.html").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted((BASE / "css").glob("*.css"))]
    parts += [p.read_text(encoding="utf-8") for p in sorted((BASE / "js").rglob("*.js"))]
    return "\n".join(parts)


SRC = _frontend_src()
CORE = (BASE / "js" / "core.js").read_text(encoding="utf-8")
WIZARD = (BASE / "setup.js").read_text(encoding="utf-8")
SIMPLE = (BASE / "simple.html").read_text(encoding="utf-8")


def body(fn: str, src: str = SRC) -> str:
    m = re.search(rf"^(?:async )?function {re.escape(fn)}\(.*?^\}}", src, re.S | re.M)
    assert m, f"{fn} not found"
    return m.group(0)


# ---------------------------------------------------------------- 共通ガード層

def test_interaction_tracking_listeners():
    """押下と IME 変換は window capture で拾う (scroll と同じ理由: バブリングしない)。"""
    for ev in ("pointerdown", "pointerup", "pointercancel"):
        assert re.search(rf'window\.addEventListener\("{ev}", \(\) => \{{ _pressing = ', CORE), ev
    for ev, val in (("compositionstart", "true"), ("compositionend", "false")):
        assert re.search(rf'window\.addEventListener\("{ev}", \(\) => \{{ _composing = {val}; \}}', CORE), ev
    assert 'window.addEventListener("blur", () => { _pressing = false; _composing = false; });' in CORE


def test_uiBusyIn_covers_input_selection_press_animation():
    b = body("uiBusyIn")
    assert "if (_pressing && Date.now() - _pressT < 5000) return true;" in b   # 押下置き去りでも更新が永久停止しない
    assert "if (_composing) return true;" in b
    # フォーカス中の編集要素 (iframe = プレビューアプリ操作中も含む) は箱の中だけ見る
    assert 'ae.matches("input,select,textarea,iframe")' in b
    assert "el.contains(ae)" in b
    # テキスト選択 = 見ている / コピーしようとしている
    assert "!sel.isCollapsed" in b and "el.contains(sel.anchorNode)" in b
    # 有限アニメーションだけ待つ (無限ループの spin / pulse / blink を数えると永遠に更新されない)
    assert "t.iterations === Infinity" in b
    assert 'a.playState === "running" || a.playState === "pending"' in b
    assert "getAnimations({subtree: true})" in b


def test_setHtml_contract():
    b = body("setHtml")
    # (a) 変更なし → 何もしない (キャッシュ比較が最初)
    assert "if (_htmlCache.get(el) === html) return false;" in b
    # (b) 操作中 / 未保存編集 → 後回し。初回 (箱が空) は後回しにしない
    hold = re.search(r"if \(el\.childElementCount && \(uiScrolling\(\) \|\| uiBusyIn\(el\) \|\| formDirty\(el\)\)\) \{", b)
    assert hold, b
    assert "deferRender(key, redo" in b
    # (c) 差し替えは snap → write → restore → stamp → cache の順
    assert b.index("logScrollSnap(el)") < b.index("el.innerHTML = html;")
    assert b.index("el.innerHTML = html;") < b.index("logScrollBack(el, _ls)")
    assert b.index("logScrollBack(el, _ls)") < b.index("stampForm(el)")
    assert b.index("stampForm(el)") < b.index("_htmlCache.set(el, html)")


def test_setOptions_contract():
    b = body("setOptions")
    assert "if (sel._optHtml === html && sel.childElementCount) return false;" in b
    # ドロップダウンを開いている (フォーカスがある) 間は組み直さない
    assert "document.activeElement === sel" in b
    assert "deferRender(key, redo" in b
    # 値は前の選択を復元 (pick があればそれ)。キャッシュは実際に書いたときだけ進める
    assert "const cur = pick != null ? pick : sel.value;" in b
    assert b.index("sel.innerHTML = html;") < b.index("sel._optHtml = html;")


def test_form_dirty_helpers():
    assert 'f.dataset.base !== undefined && _fieldVal(f) !== f.dataset.base' in body("formDirty")
    assert "f.dataset.base = _fieldVal(f)" in body("stampForm")
    assert "delete f.dataset.base" in body("formReset")


def test_saved_forms_accept_current_values():
    """保存 API が成功したら基準値を引き直す。さもないと dirty が残り続けて
    保存済みの値が描画されない (デッドロック)。リセット系は基準を捨てる。"""
    assert 'stampForm($("#oom-card"))' in body("saveOom")
    assert "stampForm(card)" in body("saveBackend")
    assert "formReset(card)" in body("resetBackend")
    assert 'formReset($("#home-par-daily"))' in body("parDailyResetTime")


def test_patchChips_only_toggles_class_when_lineup_unchanged():
    b = body("patchChips")
    assert "el._chipSig === sig && el.childElementCount === chips.length" in b
    assert 'c.classList.toggle("on", chips[i].id === onId)' in b
    assert "if (setHtml(el, html, key, redo)) el._chipSig = sig;" in b


def test_meterPatch_preserves_nodes_and_transition():
    b = body("meterPatch")
    assert "_meterNorm(el.innerHTML) !== _meterNorm(tpl.innerHTML)" in b
    v = body("_meterValues")
    assert "oi.style.width = ni.style.width" in v   # 既存ノードの width 更新 = トランジション維持
    assert "or.innerHTML = nr.innerHTML" in v


# ---------------------------------------------------------------- 各ビューの適用

def test_backends_grid_is_per_card_and_guarded():
    """グリッドごと組み直すと 1 枚の保存で他のカードの入力途中の値まで巻き戻る。"""
    b = body("renderBackends")
    # カード単位の書き込みは「中身」(外側の .card div を含まない) でないと入れ子になる
    assert 'setHtml(cards[i], bkCardInner(b), "bk-" + b.id, renderBackends)' in b
    # 顔ぶれ変更は骨格 → カード単位の 2 段 (骨格書き込みの直後にカードキャッシュが
    # 無いまま setHtml すると、次のポーリングで無変更でも 1 回組み直してしまう)
    assert 'setHtml(g, bs.map(b => `<div class="card" data-bk="${esc(b.id)}"></div>`).join(""), "backends-grid", renderBackends)' in b
    assert "g.innerHTML =" not in b


def test_oom_card_writes_via_setHtml():
    b = body("renderOomCard")
    assert 'setHtml(el, html, "oom-card", renderOomCard)' in b
    assert "el.innerHTML = `" not in b   # 毎ポーリングの無条件組み立ては禁止


def test_launch_and_profiles_grids_guarded():
    for fn, key in (("renderLaunch", "launch-grid"), ("renderProfiles", "profiles-grid")):
        b = body(fn)
        assert f'patchRows(g, html, "{key}", {fn})' in b, fn
        assert "g.innerHTML =" not in b, fn
        assert "patchChips(f, chips," in b, fn
    # 稼働時間は毎秒変わるので再構築理由にしない (in-place patch)
    assert "patchUptime(g, live)" in body("renderLaunch")


def test_home_sections_are_selective():
    for fn, key in (("renderHomeNotices", "home-notices"), ("renderHomeTiles", "home-tiles"),
                    ("renderHomeLive", "home-live"), ("renderHomeBackends", "home-backends"),
                    ("renderHomeRecent", "home-recent")):
        assert f'"{key}"' in body(fn), fn
    assert "el.innerHTML =" not in body("renderHomeNotices")
    assert "patchUptime(el, live)" in body("renderHomeLive")
    assert "patchUptime(el, ss.filter(isLive))" in body("renderHomeRecent")
    # ホスト行は load / 稼働時間だけ in-place (リンクのホバーを毎ポーリング飛ばさない)
    assert 'data-f="load"' in body("renderHomeHost") and 'data-f="upt"' in body("renderHomeHost")


def test_home_meters_and_gpus_patch_in_place():
    b = body("renderHomeMeters")
    assert "if (!meterPatch(el, out)) setHtml(el, out, \"home-meters\", renderHomeMeters);" in b
    g = body("renderHomeGpus")
    assert "c.dataset.gpu === String(s.gpus[i].uuid || s.gpus[i].index)" in g
    assert "_meterValues(m, nm[k])" in g
    assert 'setHtml(ov, nv.innerHTML, "gpu-vram-" + cards[i].dataset.gpu, renderHomeGpus)' in g
    assert 'data-gpu="${esc(g.uuid || g.index)}"' in body("gpuCardHtml")


def test_home_par_daily_window_is_separate_from_chart():
    """リセット時刻 input を 1 秒ごとのチャート容器から分離 (入力が毎秒消えていた)。"""
    assert '<div id="home-par-daily"></div>' in SRC
    b = body("renderHomeParallel")
    assert 'setOptions(sel, ' in b and '"par-sel"' in b
    assert 'setHtml(dailyEl, parDailySection(p), "par-daily", renderHomeParallel)' in b
    assert 'setHtml(el, ' in b and '"home-par"' in b
    # チャート容器の HTML に日次窓 (input を含む) を混ぜない
    assert "${parDailySection(p)}`" not in b


def test_models_holds_before_advancing_signatures():
    b = body("renderModels")
    for sig in ("S.mm._actSig = actHtml", "S.mm._sig = sig", "S.mm._catSig = cat"):
        guard = b.index("uiBusyIn(")
        assert guard < b.index(sig), sig
    assert b.count("uiBusyIn(") >= 3
    assert "patchChips(" in b


def test_coding_select_history_viewer_guarded():
    b = body("renderCoding")
    assert 'setOptions(sel, ' in b and '"cb-profile"' in b
    assert "sel.innerHTML =" not in b   # 無条件の毎ポーリング作り直し (選択が飛ぶ) は禁止
    h = body("renderCodingHistory")
    assert "if (html === S.coding._hist && el.childElementCount) return;" in h
    # 操作中ガードは残る。ただし force (ユーザーのチェック操作) は即時反映
    assert "if (!force && uiBusyIn(el) && el.childElementCount) { deferRender(\"cb-hist\", renderCodingHistory); return; }" in h
    assert "_hist: \"\"" in SRC.split("function cbInit")[1][:900]
    a = body("renderCodingActive")
    # ホールドは骨格変更時のみ。ライブ欄は cbActivePatch が選択中でも進める
    assert "if (el.childElementCount && (uiScrolling() || uiBusyIn(el))) { deferRender(\"cb-act\"," in a
    assert a.index("cbActivePatch") < a.index("uiBusyIn(el)", a.index("cbActivePatch"))
    v = body("renderCodingViewer")
    assert 'setOptions($("#cb-selA"), cbViewerOptions(d.selA), "cb-selA", renderCodingViewer)' in v
    assert 'setOptions($("#cb-selB"), cbViewerOptions(d.selB), "cb-selB", renderCodingViewer)' in v
    assert 'deferRender("cb-pane" + side, renderCodingViewer)' in v
    # ペインの sig はガードを通過してから進める (後回しにした更新を取りこぼさない)
    assert v.index("uiBusyIn(paneEl)") < v.index('d["_psig" + side] = sig')


def test_bench_calibrate_setup_selects_use_setOptions():
    for fn, keys in (("renderBench", ["bm-profile"]),
                     ("renderCalibrate", ["cal-profile"]),
                     ("renderSetup", ["ins-backend", "ins-ref", "dl-dest", "dl-model"])):
        b = body(fn)
        for k in keys:
            assert f'"{k}"' in b, (fn, k)
        assert "setOptions(" in b, fn
        assert "sel.innerHTML =" not in b and "rs.innerHTML =" not in b, fn
        assert "dd.innerHTML =" not in b and "ms.innerHTML =" not in b, fn
    assert 'setOptions(sel, ' in body("renderSetupCal") and '"sc-profile"' in body("renderSetupCal")


def test_active_and_history_panels_hold_while_busy():
    """test_bench_scroll_keep の scroll ガードはそのままに、操作中ガードを足す。
    force 引数付き (チェック操作の即時反映) は !force で同じガードを通る。"""
    for fn in ("renderBenchActive", "renderBenchHistory", "renderBenchCompare",
               "renderCalActive", "renderCalHistory", "renderInsActive", "renderInsHistory"):
        b = body(fn)
        assert "uiBusyIn(el) && el.childElementCount) { deferRender(" in b, fn
        # scroll ガード (既存契約) が先に残っている
        assert "if (uiScrolling() && el.childElementCount) { deferRender(" in b, fn


def test_playground_selects_and_messages_guarded():
    b = body("renderPlayground")
    assert '"pg-srv"' in b and '"pg-model"' in b and "setOptions(" in b
    assert "srv.innerHTML =" not in b and "mod.innerHTML =" not in b
    m = body("pgRenderMsgs")
    assert 'deferRender("pg-msgs", () => pgRenderMsgs())' in m
    assert m.index("uiBusyIn(el)") < m.index("S.pg._mSig = sig")


def test_running_and_history_hold_before_advancing_signatures():
    r = body("renderRunning")
    assert 'deferRender("run-list", () => renderRunning())' in r
    assert r.index("uiBusyIn(list)") < r.index("S._ssSig = sig")
    h = body("renderHistory")
    assert 'deferRender("hist-list", () => renderHistory())' in h
    assert h.index("uiBusyIn(list)") < h.index("S._hsSig = sig")
    assert "patchChips(fel, chips," in h


# ---------------------------------------------------------------- 別ページ (ウィザード / 簡易画面)

def test_wizard_log_and_steps_not_rewritten_unchanged():
    assert 'if (psEl._html !== stepsHtml) { psEl._html = stepsHtml; psEl.innerHTML = stepsHtml; }' in WIZARD
    assert 'if ($("#pg-log").textContent !== pgTxt) $("#pg-log").textContent = pgTxt;' in WIZARD
    assert 'if (el.textContent !== txt) el.textContent = txt;' in WIZARD
    assert 'if ($("#dl-log").textContent !== dlTxt) $("#dl-log").textContent = dlTxt;' in WIZARD


def test_simple_page_guarded():
    """5 秒ごとの自動更新でイコライザ / 登場アニメーションが最初に戻らない・
    押しているボタンが死なない。"""
    assert "function setHtml(el, html)" in SIMPLE
    assert "if (_pressing && el.childElementCount)" in SIMPLE
    assert "if (_htmlCache.get(el) === html) return;" in SIMPLE
    assert 'setHtml($("#list"), srvs.map(s => {' in SIMPLE
    assert "el.innerHTML = `" not in SIMPLE.split("function renderPlayer")[1].split("function renderAlert")[0]
    assert "setHtml(el, `" in SIMPLE.split("function renderAlert")[1]


# ---------------------------------------------------------------- 必要なオブジェクトだけの自動更新 (2026-11-24 第 2 段)
# ポーリングは「構造シグネチャが変わったときだけ構造更新を配る / 変わらなければ
# 時間駆動のライブ欄だけ進める」。一覧は行単位で、変わった行だけ組み直す。

def test_stateSig_excludes_time_varying_fields():
    b = body("stateSig")
    # 毎ポーリング動く値をシグネチャに入れると「常に変わった」扱いになり意味が無い
    assert "uptime" not in b
    assert "mem_available_gb" not in b
    assert "server_time" not in b
    # 描画に影響するフィールドは含む
    for f in ("s.status", "s.ready", "s.oom", "s.error", "p.values", "b.detection.installed"):
        assert f in b, f
    # fmtAgo の「N 分前」を 1 分ごとに更新するための分バケット
    assert "Math.floor(Date.now() / 60000)" in b


def test_refresh_dispatches_only_on_structural_change():
    b = body("refresh")
    assert "const sig = stateSig(next);" in b
    assert "const unchanged = silent && S._stateSig !== undefined && sig === S._stateSig;" in b
    assert "if (unchanged) { tickLive(); return; }" in b
    assert "_stateSig: undefined" in SRC   # 初回 (undefined) は必ず全体描画


def test_tickLive_only_advances_time_driven_fields():
    b = body("tickLive")
    assert "renderTabBadges();" in b
    assert 'patchUptime($("#running-list"), live)' in b
    assert 'patchUptime($("#launch-grid"), live)' in b
    assert 'patchUptime($("#home-live"), live)' in b
    assert 'patchUptime($("#home-recent"), live)' in b
    assert "innerHTML" not in b   # 構造には触らない


def test_patchRows_contract():
    b = body("patchRows")
    assert "if (_htmlCache.get(el) === html) return false;" in b
    # スクロール慣性だけ全体ホールド。操作 / 未保存編集は「行だけ」ホールドし、
    # 他の行の更新は続ける (1 行の選択で一覧全体が凍結する事故の修正)
    assert "if (el.childElementCount && uiScrolling())" in b
    assert "if (uiBusyIn(o) || formDirty(o)) { held = true; return; }" in b
    assert "if (held) {" in b
    # 行の比較はライブ DOM ではなく前回書いた HTML (in-place patch を差分と数えない)
    assert "if (_htmlCache.get(o) === nh) return;" in b
    assert "o.replaceWith(n);" in b
    # 差し替えた行のログ位置 / 開閉は復元
    assert b.index("logScrollSnap(o)") < b.index("o.replaceWith(n);")
    assert "logScrollBack(el, snap);" in b
    assert "stampForm(n);" in b
    # 全体差し替えのときは行キャッシュを seed (次のポーリングで行単位に動くように)
    assert "_htmlCache.set(c, news[i].outerHTML);" in b
    assert "n.dataset.row !== undefined" in b


def test_lists_carry_row_keys():
    assert 'data-row="${s.id}"' in body("sessionCard")
    assert 'data-row="${s.id}"' in body("histRow")
    assert 'data-row="more"' in body("renderHistory")   # 「さらに表示」もキー列の一員
    assert 'data-row="${p.id}"' in body("renderLaunch")
    assert 'data-row="${p.id}"' in body("renderProfiles")
    assert 'data-row="${s.id}"' in body("renderHomeLive")
    assert 'data-row="${s.id}"' in body("renderHomeRecent")
    assert 'data-row="${b.id}"' in body("renderHomeBackends")
    for fn in ("renderBenchHistory", "renderCalHistory", "renderInsHistory", "renderCodingHistory"):
        assert 'data-row="${esc(r.id)}"' in body(fn), fn
        assert "patchRows(" in body(fn), fn


def test_running_and_history_use_patchRows():
    assert 'patchRows(list, live.map(sessionCard).join(""), "run-list", () => renderRunning())' in body("renderRunning")
    assert 'patchRows(list, shown.map(histRow).join("")' in body("renderHistory")


def test_restoreLogBuffers_skips_correct_boxes():
    """行単位更新で差し替わらなかった行のログ選択を復元書き込みで壊さない。"""
    b = body("restoreLogBuffers")
    assert "if (el.dataset.len === String(buf.length) && el.textContent.length === buf.length) continue;" in b


def test_active_cards_are_cached_live_only_while_running():
    # HTML は関数に抽出され、描画側はキャッシュ一致なら書き換えない
    assert "function bmActiveCard(run)" in SRC
    assert "function calActiveCard(run)" in SRC
    assert "function cbActiveCard(run)" in SRC
    for fn, cache in (("renderBenchActive", "S.bench._ba"),
                      ("renderCalActive", "S.cal._ca"),
                      ("renderCodingActive", "S.coding._act")):
        b = body(fn)
        assert f"if (html === {cache} && el.childElementCount) return;" in b, fn
        assert f"{cache} = html;" in b, fn
    for init, key in (("S.bench = {", "_ba"), ("S.cal = {", "_ca"), ("S.coding = {", "_act")):
        m = re.search(re.escape(init) + r"[\s\S]*?\};", SRC)
        assert m and f'{key}: ""' in m.group(0), (init, key)


def test_completed_active_cards_clear_without_cache():
    """完了 (active=null) でライブカードは消える。setHtml のキャッシュ経由だと
       innerHTML 直書きのライブカードとキャッシュが同期しておらず「変更なし」と
       誤認して完了済みカードが手動更新まで残る (2026-11-24 の報告)。"""
    for fn in ("renderBenchActive", "renderCalActive", "renderCodingActive"):
        b = body(fn)
        null_branch = b.split("if (!run) {")[1].split("\n  }")[0]
        assert 'setHtml(el, ""' not in null_branch, fn
        assert 'el.innerHTML = ""' in null_branch, fn
        assert "deferRender(" in null_branch, fn   # 操作中は後回し (取りこぼさない)


def test_checkbox_toggles_restamp_baseline():
    """チェック操作は状態 (S.pm.sel / S.cmp / S.autoScroll) が正。基準を引き直さないと
    formDirty が「未保存編集」と誤認して行更新を止め続けてしまう。
    コーディングベンチの比較チェックは cbCmpSync が in-place で即時同期し、
    比較表は force 描画でフォーカス中でもすぐ反映する。"""
    assert "cbCmpSync()" in body("cbCmpToggle")
    assert "renderCodingHistory(true)" in body("cbCmpToggle")
    sync = body("cbCmpSync")
    assert 'cb.checked = S.coding.cmp.includes(row.dataset.row)' in sync
    assert 'cb.dataset.base = cb.checked ? "1" : "0"' in sync
    assert 'stampForm($("#profiles-grid"))' in body("toggleProfileSel")
    assert 'stampForm($("#profiles-grid"))' in body("selectAllProfiles")
    assert "this.dataset.base=this.checked?'1':'0'" in SRC   # logPanel の自動スクロール


# ---------------------------------------------------------------- ライブ欄はホールド中も進む (2026-11-24 第 3 段)
# 報告: コーディングベンチの実行中カードが、カード内でテキスト選択したまま見ていると
# 経過 0s / カウンタ 0 のまま手動更新まで凍結した。原因は「カード全体をホールド中に
# 書き換えない」ガードがライブ数値まで止めていたこと。修正: 骨格が同じなら data-f
# フックだけ in-place で進め、全体組み直し (ホールド対象) は骨格変更時だけにする。

def test_patchFields_helper():
    b = body("patchFields")
    assert 'tpl.content.querySelectorAll("[data-f]")' in b
    assert 'n.dataset.f === "bar"' in b and "o.style.width = n.style.width" in b
    assert 'n.dataset.f === "text"' in b and "o.textContent = n.textContent" in b


def test_coding_active_card_ticks_in_place():
    b = body("renderCodingActive")
    assert "if (el.childElementCount && cbActivePatch(el, run, html)) { S.coding._act = html; return; }" in b
    # 全体組み直しのホールドは骨格変更時だけ (null 分岐のクリアガードは別)
    assert b.index("cbActivePatch") < b.index("uiBusyIn(el)", b.index("cbActivePatch"))
    s = body("cbActiveStruct")
    for f in ("run.id", "run.status", "S.coding.logOpen", "!!p.tool_errors", "!!p.text"):
        assert f in s, f
    card = body("cbActiveCard")
    for hook in ('data-f="elapsed"', 'data-f="turns"', 'data-f="tools"', 'data-f="tokens"',
                 'data-f="steps"', 'data-f="badge"', 'data-f="text"'):
        assert hook in card, hook


def test_bench_cal_ins_active_tick_while_held():
    for fn, cardfn in (("renderBenchActive", "bmActiveCard"),
                       ("renderCalActive", "calActiveCard")):
        b = body(fn)
        tick = f"if (el.childElementCount && (uiScrolling() || uiBusyIn(el))) patchFields(el, {cardfn}(run));"
        assert tick in b, fn
        # tick は既存の scroll / busy ガード (ホールド) より前
        assert b.index(tick) < b.index("if (uiScrolling() && el.childElementCount) { deferRender("), fn
    b = body("renderInsActive")
    assert "if (el.childElementCount && (uiScrolling() || uiBusyIn(el))) patchFields(el, html);" in b
    for cardfn in ("bmActiveCard", "calActiveCard", "insActiveCard", "calActiveCardSetup"):
        c = body(cardfn)
        assert 'data-f="elapsed"' in c or 'data-f="prog"' in c, cardfn
        assert 'data-f="chips"' in c, cardfn


def test_home_par_chart_keeps_moving_while_slots_held():
    b = body("renderHomeParallel")
    assert 'el.innerHTML = `<div data-par-chart></div><div data-par-slots></div>`;' in b
    assert "if (chEl._html !== chartHtml) { chEl._html = chartHtml; chEl.innerHTML = chartHtml; }" in b
    # スロット表は .par-slots 容器を行単位で更新 (data-row は行側に付く)
    assert 'patchRows(tblEl.querySelector(".par-slots"), parSlotTable(latest, slotSeries, p), "par-slots", renderHomeParallel)' in b
    assert 'data-row="slot-${idx}"' in body("parSlotTable")
    assert 'data-row="head"' in body("parSlotTable")


def test_oom_card_ticks_while_held():
    b = body("renderOomCard")
    assert "if (el.childElementCount && (uiScrolling() || uiBusyIn(el))) patchFields(el, html);" in b
    assert b.index("patchFields(el, html)") < b.index('setHtml(el, html, "oom-card"')
    assert 'data-f="verdict"' in b and 'data-f="bar"' in b


def test_playground_streaming_tail_ticks_while_held():
    b = body("pgRenderMsgs")
    assert "if (S.pg.streaming && msgs.length && el.lastElementChild)" in b
    assert 'tpl.innerHTML = pgMsgHtml(last, msgs.length - 1);' in b
    assert b.index("uiBusyIn(el)") < b.index("S.pg._mSig = sig")


def test_job_histories_split_head_and_rows():
    """見出し (件数) と行を分離し、行は patchRows で変わったものだけ更新する。"""
    for fn, headkey in (("renderBenchHistory", "bm-hist-head"),
                        ("renderCalHistory", "cal-hist-head"),
                        ("renderInsHistory", "ins-hist-head"),
                        ("renderCodingHistory", "cb-hist-head")):
        b = body(fn)
        assert "const head =" in b, fn
        assert f'"{headkey}"' in b, fn
        assert 'el.innerHTML = `<div data-head></div><div data-rows></div>`;' in b or \
               'el.innerHTML = `<div data-head></div><div data-cmp></div><div data-rows></div>`;' in b, fn
