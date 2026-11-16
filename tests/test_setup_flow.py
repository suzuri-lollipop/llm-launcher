"""セットアップウィザードの「設定先行フロー」(全バックエンド共通) の回帰テスト。

2026-11-16 の UX 変更: 従来は ② モデル DL / セットアップ (数時間) が完了してから
③ 稼働設定・④ 接続設定の質問が出ていた — ユーザーは長い待ち時間の後に戻って
さらに質問に答える必要があった。これを **② モデル選択 → ③ 稼働設定 → ④ 接続設定
→ DL / セットアップジョブ開始 → ⑤ 調整 (任意) → 完了** に揃えた:

  - 設定をまとめて終えてから放置で完了まで行ける
  - GPU / 並列数の回答がセットアップ自体に渡る (エンジンビルド / 速度計測も
    選択カードで走る。DS4 / MM3 の推奨コンテキストは並列数込みで計算される)
  - ③ の config 書き戻し (kv / tp / gpu) はジョブ完了で config ができた時に
    自動適用 (bindSetupResult / bindDownloadResult)

後方互換: 変更前に開始された実行中ジョブの再開 / 「ダウンロード済みのモデルを
使う」即時採用は preJob=false の旧フロー (完了後に ③) を通る。

見張る対象:
  1. ② の確定は選択の控え (mdCaptureTable / W.dlReq) + ③ への遷移だけで、
     ジョブの POST を含まないこと (POST は startModelJob に集約)
  2. ④ (nt-go / nt-skip) が preJob のとき startModelJob を呼ぶこと
  3. ジョブ完了 (finishJob) が preJob のとき紐付け (bind*) して ⑤ へ進むこと
  4. ③ の pre-job 描画: config 未作成でも ② の表から質問を組み立て、ctx の
     二重質問をしないこと。Maya Qwen は --parallel 非対応なので並列数を聞かない
  5. 保存 (cf-go) は描画済みの質問セット (W.cfgRendered) を使うこと —
     Maya GLM の「GPU の使い方」を静的 RUNCFG の --tp として書かない
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "llm_launcher" / "static"
JS = (STATIC / "setup.js").read_text("utf-8")
HTML = (STATIC / "setup.html").read_text("utf-8")


def _block(start: str, end: str) -> str:
    i = JS.index(start)
    j = JS.index(end, i)
    return JS[i:j]


# ---------------------------------------------------------------- ② → ③ (ジョブはまだ)

def test_model_screen_captures_selection_without_starting_job():
    """② の「次へ」は選択を控えて ③ へ進むだけ。DL / セットアップの POST は
   startModelJob (④ の後) に集約する — 設定より先にジョブを始めない。"""
    handler = _block('$("#md-start").addEventListener("click"', "function renderDownload()")
    assert "api(" not in handler, "② の確定でジョブを開始してはいけない (設定先行)"
    assert "mdCaptureTable()" in handler
    assert "W.dlReq = {" in handler
    assert "mdEnterSettings();" in handler
    # 済みモデル (待ち時間なし) は従来の即時採用パスを通る
    assert "mdAdoptLocalTable()" in handler
    assert 'W.preJob = false' in handler


def test_settings_screens_precede_job_start():
    """③ 保存 → ④、④ 保存 / スキップ → startModelJob (preJob のとき)。"""
    cfgo = _block('$("#cf-go").addEventListener("click"', "/* ---------------------------------------------------------------- 3: 接続設定 */")
    assert 'renderNet(); go("s-net");' in cfgo
    assert "W.pendingUpdates = updates;" in cfgo      # config 書き戻しはジョブ完了後
    assert "W.setupGpus" in cfgo and "W.setupParallel" in cfgo
    ntgo = _block('$("#nt-go").addEventListener("click"', '$("#nt-skip").addEventListener')
    assert "if (W.preJob) { await startModelJob(); return; }" in ntgo
    ntskip = _block('$("#nt-skip").addEventListener', "/* ---------------------------------------------------------------- 2 実行")
    assert "if (W.preJob) { await startModelJob(); return; }" in ntskip


def test_start_model_job_sends_settings_answers():
    """ジョブ開始は ② の選択 + ③ の GPU / 並列数回答を載せる。
    Maya の並列数は Qwen 以外だけ (--parallel 非対応)。"""
    fn = _block("async function startModelJob()", "ジョブ完了後の紐付け")
    assert '/api/installs/maya-model' in fn and '/api/installs/strata-model' in fn
    assert 'api("/api/installs/download", { method: "POST", body: W.dlReq })' in fn
    assert "body.gpus = g;" in fn
    assert 'W.tableReq.family !== "qwen"' in fn
    assert "body.parallel = W.setupParallel;" in fn
    # 二重 POST 防止 (もどる で戻ってきた場合) — モデルジョブだけが対象
    assert 'mj.status === "running"' in fn


# ---------------------------------------------------------------- ジョブ完了 → 紐付け → ⑤

def test_finish_job_binds_then_goes_to_calibration():
    fn = _block("async function finishJob()", "/* 失敗 / 中断時に次へボタンを出して")
    assert "await bindSetupResult(r);" in fn
    assert "await bindDownloadResult(r);" in fn
    # preJob のときだけ紐付けパス。旧フロー (再開 / 済み採用) は ③ へ
    assert "if (W.preJob && W.cfgProfId) {" in fn
    assert 'renderConfig(); go("s-config"); return;' in fn
    assert 'renderCal(); go("s-cal"); return;' in fn


def test_bind_functions_apply_pending_updates_and_model():
    setup = _block("async function bindSetupResult(r)", "async function bindDownloadResult(r)")
    assert "vals.config = cfgPath;" in setup
    assert 'vals.engine = "strata";' in setup          # Strata の mock 既定対策は維持
    assert "W.pendingUpdates || {}" in setup
    assert 'body: { updates }' in setup
    dl = _block("async function bindDownloadResult(r)", "/* ---------------------------------------------------------------- 3: 調整 */")
    assert "resolveBasaltPack(mp, r.repo_id)" in dl
    assert "vals[modelKey] = mp;" in dl
    assert "config-json" in dl and "launch-json" in dl


# ---------------------------------------------------------------- ③ の pre-job 描画

def test_pre_job_config_questions_built_from_table():
    fn = _block("async function renderConfig()", '$("#cf-groups").addEventListener')
    # config 未作成でも ② の表から GPU / 家系情報を組み立てる
    assert "if (W.preJob && mdTableBackend() && W.tableReq) {" in fn
    assert "tp_ok: isMaya && (W.tableReq.family === \"deepseek\"" in fn
    # ctx は ② で選択済み — 二重に聞かない
    assert "if (W.preJob && mdTableBackend()) delete cfg.ctx;" in fn
    # Qwen は --parallel 非対応 (2 以上で起動が check_parallel で失敗する)
    assert 'if (W.cfgArgs.family === "qwen") delete cfg.par;' in fn
    # 保存 / 推奨 / 補足は描画済みの定義を使う (GLM の GPU 回答を --tp と誤記しない)
    assert "W.cfgRendered = cfg;" in fn
    assert JS.count("W.cfgRendered || RUNCFG[W.backend] || {}") >= 4


def test_welcome_plan_announces_settings_first():
    assert "設定をまとめて終えてから" in HTML
    assert "モデルと設定を選ぶ" in HTML
    # ④ のボタンがジョブ開始を予告する
    assert "保存してセットアップを開始" in JS
    assert "保存してダウンロードを開始" in JS
    assert "次へ (稼働設定)" in JS


# ---------------------------------------------------------------- 後方互換 (旧フロー)

def test_resumed_job_keeps_legacy_post_flow():
    boot = _block("async function boot()", "/* ---------------------------------------------------------------- 1: バックエンド選択 */")
    assert "W.preJob = false; W.modelSkipped = false;" in boot
    # 旧フローの ③→④→⑤ 経路 (config-args 即時書き戻し / 再生成) は残っている
    cfgo = _block('$("#cf-go").addEventListener("click"', "/* ---------------------------------------------------------------- 3: 接続設定 */")
    assert 'if (Object.keys(updates).length)' in cfgo
    assert "cfg.regen === \"config-args\"" in cfgo
    ntgo = _block('$("#nt-go").addEventListener("click"', '$("#nt-skip").addEventListener')
    assert 'if (!W.preJob && cfg.regen && cfg.regen !== "config-args") {' in ntgo


def test_start_model_job_ignores_the_install_job():
    """④ の後のジョブ開始は「モデルジョブ開始済み」だけを二重 POST 防止の対象にする。
    ① インストールジョブ (done) が W.job に残っていると、それを「モデルジョブ完了」と
    誤認してダウンロードを POST せず調整画面へ飛んでいた (2026-11-16 実害:
    256G を選んでもダウンロードがスキップされた。jsdom 再現で確認)。"""
    fn = _block("async function startModelJob()", "ジョブ完了後の紐付け")
    assert 'const mj = (W.job && W.job.id && (W.kind === "download" || W.job.phase === "model")) ? W.job : null;' in fn
    assert 'if (mj.status === "done") { renderCal(); go("s-cal"); return; }' in fn
    assert 'if (mj.status === "running" || mj.status === "queued") {' in fn
