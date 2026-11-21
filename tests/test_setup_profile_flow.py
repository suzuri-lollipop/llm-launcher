"""プロファイル作成 + キャリブレーションのみウィザード (/setup?flow=profile) の回帰テスト。

2026-11-16 の機能追加: 通常のセットアップウィザード (① インストール → ② モデル選択
→ ③ 稼働設定 → ④ 接続設定 → DL / セットアップ → ⑤ 調整) から、**インストール /
ダウンロードを一切行わないモード** を分離した:

  - 入口はプロファイル管理タブの「ウィザード作成」ボタン (主) とセットアップタブ /
    ウィザード歓迎画面のリンク (副)
  - バックエンド (インストール済みのみ) → ダウンロード済みモデルから選ぶ →
    ③ 稼働設定 → ④ 接続設定 → ⑤ 調整 (任意) → 完了
  - 権限は profiles.edit (+ 調整は calibrate)。install 権限は不要 — そのため
    /api/installs (install 権限) は叩かず、読み取り専用の
    GET /api/profiles/setup-options (profiles.edit) から選択肢を読む
  - Strata / Maya の済みモデル一覧は重み基準 (config の quant_key では判定しない)。
    config が無い量子化は純正セットアップが config だけ作成 (DL なし)

見張る対象:
  1. profile モードの起動 / 権限 / データ源 (install 権限・/api/installs に依存しない)
  2. インストール / DL ジョブを開始・再開しない (済みモデルの即時採用だけ)
  3. モデル選択は「ダウンロード済み」固定 (ダウンロード択 / あとでする を出さない)
  4. Strata / Maya の一覧は config 作成済みだけ。config 欠落はジョブで補わない
  5. ③④⑤ の既存コア (プロファイル作成 / 保存 / 調整) と戻り導線はそのまま再利用
  6. API: setup-options の形と RBAC (profiles.edit で読め、install 不要)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from test_auth_users import Client, auth_on, fast_kdf, make_app, setup_admin  # noqa: F401
from test_roles import add_user, login

from llm_launcher import rbac
from llm_launcher.backends import BACKENDS
from llm_launcher.installer import INSTALL_SPECS

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "llm_launcher" / "static"
JS = (STATIC / "setup.js").read_text("utf-8")
HTML = (STATIC / "setup.html").read_text("utf-8")
PROFILES_JS = (STATIC / "js" / "views" / "profiles.js").read_text("utf-8")
SETUP_VIEW_JS = (STATIC / "js" / "views" / "setup.js").read_text("utf-8")


def _block(start: str, end: str) -> str:
    i = JS.index(start)
    j = JS.index(end, i)
    return JS[i:j]


# ---------------------------------------------------------------- 起動 / 権限 / データ源

def test_flow_param_selects_profile_mode():
    assert 'flow: new URLSearchParams(location.search).get("flow") === "profile" ? "profile" : "full"' in JS
    assert 'const profileFlow = () => W.flow === "profile";' in JS


def test_profile_boot_requires_profiles_edit_not_install():
    boot = _block("async function boot()", "/* ---------------------------------------------------------------- 1: バックエンド選択 */")
    assert '(W.me.perms || {})["profiles.edit"]' in boot
    assert 'api("/api/profiles/setup-options")' in boot
    # 通常モードの install 権限チェックと /api/installs はそのまま残る
    assert "(W.me.perms || {}).install" in boot
    assert 'api("/api/installs")' in boot
    # profile モードはインストール / DL ジョブを引き継がない (調整ジョブだけ再開)
    assert "const act = profileFlow() ? null : W.installs.jobs.find(r => r.id === W.activeInstalls);" in boot
    assert "W.preJob = false; W.modelSkipped = false;" in boot   # 旧フロー再開は不変


def test_profile_chrome_renumbers_steps():
    fn = _block("function applyProfileChrome()", "/* ---------------------------------------------------------------- 画面遷移 */")
    assert '"ステップ 1 / 4 — バックエンドとモデル"' in fn
    assert '"ステップ 2 / 4 — 稼働設定"' in fn
    assert '"ステップ 3 / 4 — 接続設定"' in fn
    assert '"ステップ 4 / 4 — 調整"' in fn
    assert 'const calKicker = () => profileFlow() ? "ステップ 4 / 4 — 調整" : "ステップ 5 / 5 — 調整";' in JS


# ---------------------------------------------------------------- バックエンド選択

def test_backend_screen_gates_on_installed():
    fn = _block("function renderBackends()", "function renderBackends_next()")
    assert "W.installedMap" in fn
    assert "インストール済み" in fn and "未インストール" in fn
    assert '${ok ? "" : " disabled"}' in fn
    nxt = _block("function renderBackends_next()", "/* ---------------------------------------------------------------- 1b: ブランチ */")
    assert "profileFlow() && !W.installedMap[W.backend]" in nxt
    # profile モードの「次へ」はインストール確認 / ブランチ選択へ行かずモデル選択へ
    handler = _block('$("#be-next").addEventListener("click"', "/* ---------------------------------------------------------------- 1b: ブランチ */")
    assert 'if (profileFlow()) { renderModel(); go("s-model"); return; }' in handler


# ---------------------------------------------------------------- モデル選択 (済みモデルのみ)

def test_model_screen_is_local_only():
    fn = _block("function mdForceLocal()", "async function renderModel()")
    assert 'localR.checked = true;' in fn
    assert '$("#md-modes").classList.add("hidden");' in fn
    assert '$("#md-later").classList.add("hidden");' in fn
    assert "async function renderModel() {\n  mdForceLocal();" in JS
    assert "async function renderModelTable(backend) {\n  mdForceLocal();" in JS
    # 済みモデル 0 件でもダウンロード択へ切り替えない (profile モード)
    assert JS.count('mdMode() === "local" && !profileFlow()') == 2
    # 「あとでする」は mdSync 経由の再表示でも profile モードでは隠れたまま
    assert 'profileFlow() || backend === "strata" || backend === "maya"' in JS


def test_profile_list_is_weights_based_not_config_based():
    """済みモデル一覧は「重みがディスクに揃っているか」(表の have) で決める。
    config JSON の有無 / quant_key で決めない — 共有 config (maya-ds4.json) の
    quant_key が最後の設定を指すと、他の量子化の重みが在っても一覧から消える
    (2026-11-16 実害: 128G の重みが在るのに 256G しか選べなかった)。"""
    fn = _block("async function renderModelTable(backend)", "function mdStFullIn(")
    assert "if (mdStFullIn(f, m)) dl.push(" in fn
    assert "st.downloaded = dl;" in fn
    # config の quant_key / weights.ok による一覧フィルタは無い
    assert "y.quant_key === x.model" not in fn
    assert "const ready = profileFlow()" not in fn
    assert "ダウンロード済みのモデルがありません — 先に通常のセットアップでダウンロードしてください" in fn


def test_adopt_local_creates_config_when_weights_present():
    """重みが在って config が無い量子化は、純正セットアップジョブで config だけ
    作る (ダウンロードなし)。profile モードも同じ導線 — 一覧が重み基準なので
    「config が無い = 選べない / エラーで止める」にはしない。"""
    fn = _block("async function mdAdoptLocalTable()", '$("#md-start").addEventListener')
    # config 一致 → 即時採用 (ジョブなし) は維持
    assert 'W.job = { id: "", kind: "install", phase: "model", status: "done"' in fn
    assert 'W.preJob = false; W.modelSkipped = false;' in fn
    # config 不一致 → tableReq + 設定先行フロー (ジョブは ④ の後・DL なし)
    assert "W.tableReq = { family: x.fam, model: x.model," in fn
    assert "mdEnterSettings();" in fn
    # profile モード専用のエラー停止ブロックは無い
    assert "このモデルの config JSON が見つかりません" not in fn


def test_profile_mode_reads_tables_from_options_not_installs_api():
    st = _block("async function loadStrata()", "/* 現在バックエンドの表")
    assert "W.mdStrata = (W.options && W.options.strata)" in st
    my = _block("async function loadMaya()", "/* 現在バックエンドの表")
    assert "W.mdMaya = (W.options && W.options.maya)" in my


# ---------------------------------------------------------------- ③④⑤ の再利用と完了

def test_config_and_cal_core_reused():
    # ③ の保存コア (PUT → config-args / launch-json / config-json) は不変
    cfgo = _block('$("#cf-go").addEventListener("click"', "/* ---------------------------------------------------------------- 3: 接続設定 */")
    assert 'await api(`/api/profiles/${p.id}`, { method: "PUT", body });' in cfgo
    assert 'cfg.regen === "config-args"' in cfgo
    # プロファイルの note だけモードで出し分け
    assert 'note: profileFlow() ? "プロファイルウィザードで作成" : "初回セットアップで作成"' in JS
    # ⑤ は calibrate 権限 / 実行中ジョブを正直に見る (403 を「非対応」と誤読しない)
    cal = _block("async function renderCal()", "function syncCalSel()")
    assert "const calOff = !!c && !supported.includes(W.backend);" in cal
    assert "if (canCal && calOff) {" in cal
    assert "!W.busyInstall" in cal
    assert "キャリブレーション権限のあるアカウントで実行できます" in cal
    assert "セットアップジョブが実行中です" in cal
    # 調整開始の kicker はモード対応
    cago = _block('$("#ca-go").addEventListener("click"', "/* ---------------------------------------------------------------- 完了 */")
    assert "setProgChrome(calKicker()" in cago


def test_back_navigation_and_welcome():
    assert 'const BACK_PROFILE = { "s-backend": "s-welcome-profile", "s-model": "s-backend",' in JS
    assert '"s-config": "s-model", "s-net": "s-config", "s-cal": "s-net" };' in JS
    back = _block("function goBack()", "/* ---------------------------------------------------------------- 起動 */")
    assert "if (profileFlow()) {" in back
    assert 'id="s-welcome-profile"' in HTML
    assert "プロファイルを作って調整します" in HTML
    assert "新しいダウンロードは起きません" in HTML
    # 完了画面はプロファイル管理への導線を持つ
    assert 'id="dn-title"' in HTML and 'id="dn-profiles"' in HTML
    assert 'href="/#/profiles"' in HTML


# ---------------------------------------------------------------- 入口 (プロファイル管理 / セットアップ)

def test_dashboard_entries():
    # 主入口: プロファイル管理タブ (profiles.edit = need-edit で権限表示)
    assert 'function openProfileWizard() { location.href = "/setup?flow=profile"; }' in PROFILES_JS
    assert 'onclick="openProfileWizard()"' in PROFILES_JS
    assert "ウィザード作成" in PROFILES_JS
    assert PROFILES_JS.count("need-edit\" title=\"ガイド付き") == 1
    # 副入口: セットアップタブのカード
    assert "/setup?flow=profile" in SETUP_VIEW_JS
    assert "プロファイル作成 + キャリブレーションのみ" in SETUP_VIEW_JS
    # ウィザード歓迎画面 (通常モード) からの相互導線
    assert 'href="/setup?flow=profile"' in HTML
    assert 'href="/setup"' in HTML


# ---------------------------------------------------------------- API / RBAC

def test_required_cap_is_profiles_edit():
    assert rbac.required_cap("GET", "/api/profiles/setup-options") == "profiles.edit"
    # 既存の install 権限境界は変わらない
    assert rbac.required_cap("GET", "/api/installs") == "install"
    assert rbac.required_cap("GET", "/api/installs/strata-model") == "install"


def test_setup_options_shape(tmp_path):
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        st, j = await c.get("/api/profiles/setup-options")
        assert st == 200, j
        assert set(j) == {"installable", "installed", "strata", "maya", "cache_dir", "busy"}
        ids = [s["id"] for s in j["installable"]]
        assert ids == [bid for bid in INSTALL_SPECS if bid in BACKENDS]
        assert set(j["installed"]) == set(ids)
        assert all(isinstance(v, bool) for v in j["installed"].values())
        assert j["busy"] is False
        # ジョブ履歴 / ログは含まない (読み取り専用の選択肢だけ)
        assert "jobs" not in j and "active" not in j
        for key in ("strata", "maya"):
            t = j[key]
            assert "available" in t and "families" in t and "configs" in t and "venv_ready" in t

    asyncio.run(go())


def test_setup_options_needs_profiles_edit_not_install(tmp_path, auth_on, fast_kdf):
    """install 権限が無いメンテナーでも profiles.edit があれば選択肢を読める
    (プロファイル作成 + 調整のみフローの権限要件)。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        await add_user(admin, "hana", "maintainer")
        hana = await login(app, "hana")
        # 既定: profiles.edit / install とも OFF
        st, _ = await hana.get("/api/profiles/setup-options")
        assert st == 403
        # profiles.edit だけ ON (install は OFF のまま)
        st, _ = await admin.put("/api/roles/maintainer", {"perms": {"profiles.edit": True}})
        assert st == 200
        st, j = await hana.get("/api/profiles/setup-options")
        assert st == 200 and "installable" in j
        st, _ = await hana.get("/api/installs")          # install 境界は変わらない
        assert st == 403
        st, _ = await hana.get("/api/calibrations")      # calibrate 未開示 → ⑤ はスキップ導線
        assert st == 403
        # 管理者は常に読める
        st, _ = await admin.get("/api/profiles/setup-options")
        assert st == 200

    asyncio.run(go())


def test_profile_loaders_populate_mdt():
    """profile モードの loadStrata / loadMaya は W.mdStrata / W.mdMaya にも表を入れる。
    mdT() / mdAdoptLocalTable がここを参照するため、入れないと済みモデル選択が
    無反応で先に進めない (2026-11-16 実害: 「このモデルで続ける」が効かなかった)。"""
    st = _block("async function loadStrata()", "/* 現在バックエンドの表")
    assert "W.mdStrata = (W.options && W.options.strata)" in st
    my = _block("async function loadMaya()", "/* 現在バックエンドの表")
    assert "W.mdMaya = (W.options && W.options.maya)" in my


def test_downloaded_option_labels_frontload_quant():
    """済みモデルの選択肢は量子化キー + 実サイズを先頭に置く — 家族名が先頭を
    占めて末尾だけ違うモデル (Mix-128G / Mix-256G) が見分けられない問題の修正。"""
    fn = _block("async function renderModelTable(backend)", "function mdStFullIn(")
    assert 'esc(x.model + " · " + (x.gb || "?") + " GB — " + x.label)' in fn
    # 選択中の完全名はヒント (md-dest) にも出す
    sync = _block("function mdSync()", "async function renderModel()")
    assert '${sel.label} — ${sel.gb || "?"} GB · 再取得なし' in sync


# ---------------------------------------------------------------- コンテキスト選択 (2026-11-17 修正)

def test_adopt_without_config_asks_context_in_settings():
    """config 無し済みモデルの採用はコンテキストを ② で選んでいない (profile
    モードの ② は済みモデル一覧だけ)。それなのに ③ が「② で選択済み」として
    ctx 質問を削除すると、コンテキスト長を選ぶ画面がどこにも無くなる
    (2026-11-17 報告: strata でコンテキストサイズが選択できない)。
    tableCtxPick で ③ に質問を残し、回答をセットアップジョブへ渡す。"""
    fn = _block("async function mdAdoptLocalTable()", '$("#md-start").addEventListener')
    # config 無し → ③ で聞く。推奨 (rec_context) は既定として控える
    assert "W.tableCtxPick = true;" in fn
    assert 'context: d.rec_context || (W.backend === "maya" ? 32768 : 131072), vision: "" };' in fn
    # config 一致の即時採用はフラグを立てない (③ が既存 config をプリフィル)
    assert "W.tableCtxPick = false;" in fn
    # ② の面板で選択済みの経路 (ダウンロード択) は二重に聞かない
    cap = _block("function mdCaptureTable()", "function mdEnterSettings()")
    assert "W.tableCtxPick = false;" in cap


def test_settings_ctx_question_uses_family_whitelist():
    """③ が聞くコンテキストの選択肢はモデル別 (Hy4-Flash / GLM-5.3) か家系の
    ホワイトリスト (② の面板と同じ表)。一覧外の値はセットアップジョブの
    build_argv が拒否するので出さない。プリフィルは ② が控えた推奨値 (rec_context)。"""
    fn = _block("async function renderConfig()", '$("#cf-groups").addEventListener')
    assert "if (W.preJob && mdTableBackend() && !W.tableCtxPick) delete cfg.ctx;" in fn
    assert "if (W.preJob && mdTableBackend() && W.tableCtxPick && cfg.ctx && W.tableReq) {" in fn
    assert "const src = (md.contexts || []).length ? md.contexts" in fn
    assert ": ((fam.contexts || []).length ? fam.contexts : (st.contexts || []));" in fn
    assert "if (cs.length) {" in fn
    assert 'cfg.ctx = { ...cfg.ctx, opts: cs.map(c => [String(c), CF_TOK(c), ""]) };' in fn
    assert 'if (W.tableCtxPick) W.cfgArgs["--max-context"] = String(W.tableReq.context || "");' in fn
    # ホワイトリスト外の保存値 (家系乗り換え等) は推奨へ戻す — ジョブが拒否するため
    assert "if (ctxCs && curs.ctx && !ctxCs.includes(Number(curs.ctx)))" in fn


def test_settings_ctx_answer_goes_to_setup_job():
    """③ のコンテキスト回答は tableReq.context に入ってセットアップジョブへ渡る。
    ジョブ後の config-args 書き戻しだけだと、純正 setup が推奨値に対して計算した
    rope scaling / KV 予約などがズレた config になる。"""
    cfgo = _block('$("#cf-go").addEventListener("click"', "/* ---------------------------------------------------------------- 3: 接続設定 */")
    assert "if (W.tableCtxPick && W.tableReq && vals.max_context)" in cfgo
    assert "W.tableReq.context = parseInt(vals.max_context, 10) || W.tableReq.context;" in cfgo
    # ジョブ開始は tableReq をそのまま送る (context 込み)
    job = _block("async function startModelJob()", "ジョブ完了後の紐付け")
    assert "const body = { ...W.tableReq };" in job


def test_profile_model_screen_announces_context_step():
    """モデル選択画面の案内が「コンテキストは次の稼働設定で選ぶ」を予告する。"""
    fn = _block("async function renderModelTable(backend)", "function mdStFullIn(")
    assert "コンテキスト長は次の稼働設定で選びます" in fn
