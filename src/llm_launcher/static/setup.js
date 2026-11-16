/* ============================================================
   セットアップ ウィザード (Google ログイン画面風の 1 画面 1 質問)
   index.html のセットアップタブから /setup として遷移してくる専用ページ。
   API は既存のものだけを叩く: /api/installs (clone/venv/pip・HF DL) と
   /api/calibrations (調整)。画面遷移は .step.active の付け替え + CSS の
   slide-in アニメ。ジョブは fire-and-forget + 2 秒ポーリング (index と同じ)。
   ============================================================ */
"use strict";

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* ---------------------------------------------------------------- API */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    credentials: "same-origin",
    headers: opts.body ? { "content-type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let j = null;
  try { j = await r.json(); } catch { /* 非 JSON は本文なしで扱う */ }
  if (!r.ok) {
    const e = new Error((j && (j.detail || j.message)) || `HTTP ${r.status}`);
    e.status = r.status;
    throw e;
  }
  return j;
}

/* ---------------------------------------------------------------- 状態 */
const INS_STEPS = { clone: "リポジトリを取得 (clone)", branch: "ブランチ切替", venv: "実行環境を作成",
                    precheck: "インストール内容を確認", cmake: "エンジンを設定 (cmake)",
                    "cmake-build": "エンジンをビルド (cmake)", engine: "エンジンを受信 (公式リリース)",
                    tools: "依存 + llama.cpp ソースを導入 (純正 tools_step)",
                    model: "モデルを準備 (純正セットアップ)", verify: "動作確認 (import)" };
const CAL_STEPS = { prepare: "調整の準備", auto: "最適構成を自動で探す", lookup: "キャッシュを引く",
                    sweep: "実際に測る (実測)", create: "プロファイルを更新", adjust: "調整を反映",
                    calibrate: "実測調整 (CPU レーン / TP)", speed: "drafter 速度計測 (DSpark / MTP)",
                    restart: "サーバーを起動し直す" };
const STEP_NAMES = { queued: "待機しています", running: "", done: "完了", failed: "失敗",
                     cancelled: "中断しました", lost: "中断されました (サーバー再起動)" };
const bytes = n => { n = n || 0;
  return n >= 2**30 ? (n / 2**30).toFixed(2) + " GiB"
       : n >= 2**20 ? (n / 2**20).toFixed(1) + " MB" : Math.round(n / 1024) + " KiB"; };

const W = {
  me: null, installs: { jobs: [], installable: [], tool: "uv", cacheDir: "" },
  /* flow=profile: プロファイル作成 + キャリブレーションだけのモード。
     インストール / モデルDL は行わず、ダウンロード済みモデルから選んで
     ③ 稼働設定 → ④ 接続設定 → ⑤ 調整 だけを通す (権限は profiles.edit + calibrate)。 */
  flow: new URLSearchParams(location.search).get("flow") === "profile" ? "profile" : "full",
  options: null,          // profile モードの /api/profiles/setup-options 応答
  installedMap: {},       // profile モード: バックエンド別インストール検出
  busyInstall: false,     // profile モード: セットアップジョブ実行中 (調整は開始できない)
  backend: "", ref: "", clean: false,
  kind: "", job: null, timer: null, logCursor: 0, logText: "",
  ema: null, tLast: 0, dLast: 0, growT: 0,
  cancelAt: 0,            // 中断要求を出した時刻 (強制停止ボタンの表示判定)
  cleanupShown: false,    // 中断残骸の削除画面は 1 ジョブ 1 回だけ出す
  cfgSkip: true, calProfile: null,
  cfgProfId: null, cfgProfNew: false, backends: null,
  /* 設定先行フロー (全バックエンド共通): ② モデル選択 → ③ 稼働設定 → ④ 接続設定 →
     DL / セットアップジョブ開始 → ⑤ 調整。preJob=true は「ジョブより先に設定を
     集めている」状態。② の選択は dlReq / tableReq に控え、ジョブ開始は ④ の後
     (startModelJob)。③ の config 書き戻し値は pendingUpdates に溜め、ジョブ完了で
     config ができた時に適用する (bindSetupResult / bindDownloadResult)。
     旧フロー (ジョブ完了後に ③: 実行中ジョブの再開 / 済みモデルの即時採用) は
     preJob=false のまま従来の経路を通る。 */
  preJob: false, modelSkipped: false,
  dlReq: null, tableReq: null,
  pendingUpdates: null, setupGpus: "", setupParallel: 0,
  cfgRendered: null,      // ③ が実際に描画した質問セット (家系別の削除・差し替え込み)
  summary: [],
};
const spec = id => (W.installs.installable || []).find(e => e.id === (id ?? W.backend)) || null;
const specName = id => (spec(id) || {}).name || id || "";
const profileFlow = () => W.flow === "profile";
const calKicker = () => profileFlow() ? "ステップ 4 / 4 — 調整 (任意)" : "ステップ 5 / 5 — 調整 (任意)";
/* profile モードの画面 chrome: ステップ番号 / 完了画面の文言 (インストール / DL 画面は出ない) */
function applyProfileChrome() {
  document.title = "プロファイル作成 — LLM Launcher";
  $("#be-kicker").textContent = "ステップ 1 / 4 — バックエンドとモデル";
  $("#md-kicker").textContent = "ステップ 1 / 4 — バックエンドとモデル";
  $("#cf-kicker").textContent = "ステップ 2 / 4 — 稼働設定";
  $("#nt-kicker").textContent = "ステップ 3 / 4 — 接続設定";
  $("#ca-kicker").textContent = "ステップ 4 / 4 — 調整 (任意)";
  $("#dn-title").textContent = "プロファイルの作成と調整が完了しました";
  $("#dn-sub").textContent = "ダッシュボードの起動タブからプロファイルを起動できます。調整結果はプロファイル / 起動 JSON に書き戻し済みです。";
  $("#dn-profiles").classList.remove("hidden");
  const ls = $("#login-sub");
  if (ls) ls.innerHTML = "プロファイル作成には <b>プロファイル編集権限</b> を持つアカウントが必要です。";
}
/* install ステップの実体は uv があれば uv pip、無い環境だけ venv の pip。venv_pip の
   バックエンド (Strata) は uv が在っても pip を使うので、spec / ジョブが返す tool を優先する
   (UI の手順名は実際に走る argv と同じ判定から出す)。 */
const toolLabelFor = t => (t || W.installs.tool) === "pip" ? "pip install" : "uv pip install";
const toolLabel = sp => toolLabelFor((sp || spec() || {}).tool);
const stepLabel = (name, kind) => kind === "install"
  ? (name === "install" ? toolLabelFor((W.job || {}).tool) : (INS_STEPS[name] || name))
  : (CAL_STEPS[name] || name);

/* ---------------------------------------------------------------- 画面遷移 */
let curStep = "";
function go(id, back = false) {
  if (curStep === id) return;
  if (curStep) { const prevEl = $("#" + curStep);
                 if (prevEl) prevEl.classList.remove("active", "back"); }
  curStep = id;
  const el = $("#" + id);
  el.classList.toggle("back", !!back);
  el.classList.add("active");
  window.scrollTo({ top: 0 });
  if (id !== "s-progress" && id !== "s-download") stopPoll();
}
document.addEventListener("click", e => {
  const t = e.target.closest("[data-go],[data-back]");
  if (!t) return;
  if (t.dataset.go) {
    const dest = t.dataset.go;
    go(dest);
    if (dest === "s-model") renderModel();       // 画面に入った時に選択肢を組み立てる
    else if (dest === "s-config") renderConfig();
    else if (dest === "s-net") renderNet();
    else if (dest === "s-cal") renderCal();
  }
  else if (t.hasAttribute("data-back")) goBack();
});
/* 「もどる」は論理的な前ステップへ (ラン中画面からは戻れない) */
const BACK = { "s-backend": "s-welcome", "s-branch": "s-backend", "s-review": "s-backend",
               "s-model": "s-installed", "s-config": "s-model", "s-net": "s-config",
               "s-cal": "s-net", "s-installed": "s-backend", "s-cleanup": "s-model" };
/* profile モードの「もどる」: インストール / DL 画面が無いので backend → model → 設定 の鎖 */
const BACK_PROFILE = { "s-backend": "s-welcome-profile", "s-model": "s-backend",
                       "s-config": "s-model", "s-net": "s-config", "s-cal": "s-net" };
function goBack() {
  if (profileFlow()) {
    if (curStep === "s-cal" && W.cfgSkip !== false) { go("s-model", true); renderModel(); return; }
    const dest = BACK_PROFILE[curStep] || "s-welcome-profile";
    go(dest, true);
    if (dest === "s-backend") renderBackends();
    else if (dest === "s-model") renderModel();
    else if (dest === "s-config") renderConfig();
    else if (dest === "s-net") renderNet();
    return;
  }
  if (curStep === "s-branch") { go("s-backend", true); return; }
  if (curStep === "s-review") { go(spec() && (spec().refs || []).length > 1 ? "s-branch" : "s-backend", true); return; }
  if (curStep === "s-cal" && W.cfgSkip !== false) { go("s-model", true); renderModel(); return; }
  go(BACK[curStep] || "s-welcome", true);
  if (curStep === "s-model") renderModel();
  else if (curStep === "s-config") renderConfig();
  else if (curStep === "s-net") renderNet();
}

/* ---------------------------------------------------------------- 起動 */
async function boot() {
  try {
    W.me = await api("/api/auth/status");
  } catch { go("s-login"); return; }
  if (W.me.auth_enabled && !W.me.authenticated) { go("s-login"); return; }
  const admin = W.me.role === "admin" || (!W.me.auth_enabled);
  if (profileFlow()) {
    // プロファイル作成 + 調整のみ: install 権限は不要 (profiles.edit。調整は ⑤ で calibrate を見る)
    if (!admin && !(W.me.perms || {})["profiles.edit"]) {
      $("#perm-sub").textContent = "プロファイル作成にはプロファイル編集権限のあるアカウントが必要です。"
        + "調整 (キャリブレーション) は別途キャリブレーション権限が必要です。";
      go("s-perm"); return;
    }
    applyProfileChrome();
  } else if (!admin && !(W.me.perms || {}).install) { go("s-perm"); return; }
  let inst = null;
  try {
    if (profileFlow()) {
      // 読み取り専用の選択肢 (profiles.edit)。/api/installs (install 権限) は叩かない
      const pair = await Promise.all([api("/api/profiles/setup-options").catch(() => null),
                                      api("/api/calibrations").catch(() => null)]);
      inst = pair[0];
      W.activeCal = pair[1] && pair[1].active;
      if (inst) {
        W.options = inst;
        W.installedMap = inst.installed || {};
        W.busyInstall = !!inst.busy;
        W.installs = { jobs: [], installable: inst.installable || [], tool: "uv", cacheDir: inst.cache_dir || "" };
      }
    } else {
      const pair = await Promise.all([api("/api/installs").catch(() => null),
                                      api("/api/calibrations").catch(() => null)]);
      inst = pair[0];
      W.activeInstalls = pair[0] && pair[0].active;
      W.activeCal = pair[1] && pair[1].active;
    }
  } catch { /* 下で inst を見て判定 */ }
  if (!inst) { go(W.me.auth_enabled ? "s-login" : (profileFlow() ? "s-welcome-profile" : "s-welcome")); return; }
  if (!profileFlow())
    W.installs = { jobs: inst.jobs || [], installable: inst.installable || [], tool: inst.tool || "uv", cacheDir: inst.cache_dir || "" };
  const pre = new URLSearchParams(location.search).get("backend");
  if (pre && spec(pre)) W.backend = pre;

  /* 実行中のジョブがあればその画面へ素直に戻る (リロード対策)。cal は長時間走るので必須。
     profile モードはインストール / DL ジョブを引き継がない (このフローのジョブは調整だけ) */
  const live = r => r && (r.status === "running" || r.status === "queued");
  const act = profileFlow() ? null : W.installs.jobs.find(r => r.id === W.activeInstalls);
  if (live(act)) {
    W.kind = act.kind === "download" ? "download" : "install";
    W.job = act; W.backend = act.backend || W.backend;
    // 再開したジョブは旧フロー (完了後に ③ 稼働設定) で最後まで通す
    W.preJob = false; W.modelSkipped = false;
    if (W.kind === "download") { renderDownload(); go("s-download"); }
    else {
      if (act.phase === "model")
        setProgChrome("ステップ 2 / 5 — モデル", `${specName(act.backend)} がモデルを準備しています (時間がかかります)`);
      renderProgress(); go("s-progress");
    }
    startPoll();
    return;
  }
  if (W.activeCal) {
    try {
      const cal = await api("/api/calibrations/" + W.activeCal);
      if (live(cal)) {
        W.kind = "cal"; W.job = cal;
        setProgChrome(calKicker(), "この PC に調整しています…");
        renderProgress(); go("s-progress"); startPoll();
        return;
      }
    } catch { /* 取得失敗時は通常の導入導線へ */ }
  }
  renderBackends();
  if (pre && spec(pre)) go("s-backend"); else go(profileFlow() ? "s-welcome-profile" : "s-welcome");
}

/* ---------------------------------------------------------------- 1: バックエンド選択 */
function renderBackends() {
  const list = $("#be-list");
  const specs = W.installs.installable || [];
  if (!specs.length) {
    list.innerHTML = `<p class="hint">UI からインストールできるバックエンドがありません。<br>管理者に連絡するか、ターミナルでセットアップしてください。</p>`;
    $("#be-note").textContent = "";
    $("#be-next").disabled = true;
    return;
  }
  if (profileFlow()) {
    /* プロファイル作成 + 調整のみ: インストール済みのバックエンドだけ選べる
       (このフローはインストール / ダウンロードを一切行わない) */
    if (!spec(W.backend) || !W.installedMap[W.backend])
      W.backend = (specs.find(s => W.installedMap[s.id]) || {}).id || "";
    list.innerHTML = specs.map(s => {
      const ok = !!W.installedMap[s.id];
      return `
      <label class="opt${s.id === W.backend ? " sel" : ""}"${ok ? "" : ' style="opacity:.45"'}>
        <input type="radio" name="be" value="${esc(s.id)}"${s.id === W.backend ? " checked" : ""}${ok ? "" : " disabled"}>
        <span class="txt"><span class="t1">${esc(s.name)}</span>
          <span class="t2">${esc(s.module_path)}</span></span>
        <span class="tag${ok ? "" : " no"}">${ok ? "インストール済み" : "未インストール"}</span>
      </label>`;
    }).join("");
    list.querySelectorAll("input").forEach(r => r.addEventListener("change", () => {
      W.backend = r.value;
      list.querySelectorAll(".opt").forEach(o => o.classList.toggle("sel", o.contains(r) && r.checked));
      renderBackends_next();
    }));
    $("#be-note").textContent = W.backend ? ""
      : "インストール済みのバックエンドが見つかりません。先に通常のセットアップでインストールしてください。";
    renderBackends_next();
    return;
  }
  if (!spec(W.backend)) W.backend = (specs.find(s => !s.cloned) || specs[0]).id;
  list.innerHTML = specs.map(s => `
    <label class="opt${s.id === W.backend ? " sel" : ""}">
      <input type="radio" name="be" value="${esc(s.id)}"${s.id === W.backend ? " checked" : ""}>
      <span class="txt"><span class="t1">${esc(s.name)}</span>
        <span class="t2">${esc(s.module_path)}</span></span>
      <span class="tag${s.cloned ? "" : " no"}">${s.cloned ? "取得済み" : "未インストール"}</span>
    </label>`).join("");
  list.querySelectorAll("input").forEach(r => r.addEventListener("change", () => {
    W.backend = r.value;
    list.querySelectorAll(".opt").forEach(o => o.classList.toggle("sel", o.contains(r) && r.checked));
    renderBackends_next();
  }));
  const sp = spec();
  $("#be-note").textContent = sp && sp.cloned && sp.current_ref
    ? `現在のバージョン: ${sp.current_ref}` : "";
  renderBackends_next();
}
function renderBackends_next() {
  $("#be-next").disabled = !spec() || (profileFlow() && !W.installedMap[W.backend]);
  $("#be-next").dataset.go = "";
}
$("#be-next").addEventListener("click", () => {
  const sp = spec(); if (!sp) return;
  // profile モードはインストール / ブランチ選択なし — すぐモデル選択 (済みモデルのみ) へ
  if (profileFlow()) { renderModel(); go("s-model"); return; }
  const refs = sp.refs || [];
  W.ref = pickRef(sp);
  if (refs.length > 1) { renderBranches(); go("s-branch"); }
  else renderReview();
});

/* ---------------------------------------------------------------- 1b: ブランチ */
function pickRef(sp) {
  const refs = sp.refs || [];
  if (!refs.length) return sp.ref || "";
  if (refs.some(r => r.ref === sp.current_ref)) return sp.current_ref;
  if (refs.some(r => r.ref === sp.ref)) return sp.ref;
  return refs[0].ref;
}
function renderBranches() {
  const sp = spec(); const refs = (sp.refs || []);
  $("#br-sub").innerHTML = `<b>${esc(sp.name)}</b> をどのバージョンで導入するか選んでください。`;
  const list = $("#br-list");
  list.innerHTML = refs.map(r => `
    <label class="opt${r.ref === W.ref ? " sel" : ""}">
      <input type="radio" name="br" value="${esc(r.ref)}"${r.ref === W.ref ? " checked" : ""}>
      <span class="txt"><span class="t1">${esc(r.label)}</span>
        <span class="t2 mono">${esc(r.ref)}</span></span>
      ${r.ref === sp.current_ref ? `<span class="tag">設定済み</span>` : ""}
    </label>`).join("");
  list.querySelectorAll("input").forEach(r => r.addEventListener("change", () => {
    W.ref = r.value;
    list.querySelectorAll(".opt").forEach(o => o.classList.toggle("sel", o.contains(r) && r.checked));
  }));
  $$("button[data-go='s-review']").forEach(b => b.onclick = () => renderReview());
}

/* ---------------------------------------------------------------- 1c: 確認 */
function renderReview() {
  const sp = spec(); if (!sp) { go("s-welcome"); return; }
  $("#rv-title").textContent = `${sp.name} をインストール`;
  const doing = !sp.cloned ? `新しいバージョン (${W.ref || sp.ref || "既定"}) を取得`
    : (sp.current_ref && sp.current_ref !== W.ref && W.ref ? `バージョンを ${sp.current_ref} → ${W.ref} に変更` : "取得済みのまま進める");
  const rows = [
    ["ソフト", `${esc(sp.name)} <span class="mono">${esc(sp.module_path)}</span>`],
    ["バージョン", W.ref ? esc(W.ref) : "既定"],
    ["実行内容", `1. リポジトリを取得 (git clone)<br>2. 実行環境を作成 (.venv)<br>`
      + (sp.tools_step ? "3. 依存 + llama.cpp ソースを導入 (純正 tools_step)"
         : `3. 依存パッケージを導入 (${sp.requirements ? `${toolLabel(sp)} -r ${esc(sp.requirements)}` : esc(toolLabel(sp))})`)
      + (sp.cmake ? "<br>4. ネイティブエンジンをビルド (cmake → bin/basalt)" : "")
      + (sp.engine_fetch ? "<br>4. ネイティブエンジンを受信 (公式リリース → engine/strata)" : "")],
    ["この後の予定", doing],
  ];
  if (sp.tools_step) rows.push(["エンジンビルド", "② のモデル設定で家系別に実行します (初回 10〜30 分・導入済みならスキップ)"]);
  $("#rv-rows").innerHTML = rows.map(([k, v]) =>
    `<div class="row-item"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  $("#rv-preview").textContent = `git clone → .venv → `
    + (sp.tools_step ? "純正 tools_step (pip + llama.cpp ソース)"
       : (sp.requirements ? `${toolLabel(sp)} -r ${sp.requirements}` : `${toolLabel(sp)} -e ".[${sp.extras || "accel"}]"`))
    + (sp.cmake ? " → cmake configure + build" : "")
    + (sp.engine_fetch ? " → エンジン受信 (公式リリース)" : "");
  $("#rv-start").disabled = false;
  $("#rv-start").textContent = "インストール";
  go("s-review");
}
$("#rv-start").addEventListener("click", async () => {
  const sp = spec(); if (!sp) return;
  const btn = $("#rv-start");
  btn.disabled = true; btn.textContent = "開始中…";
  try {
    const job = await api("/api/installs", { method: "POST",
      body: { backend: sp.id, clean: $("#rv-clean").checked === true, ref: W.ref || "" } });
    W.kind = "install"; W.job = job;
    W.logCursor = 0; W.logText = "";
    renderProgress(); go("s-progress"); startPoll();
  } catch (e) {
    alert("開始できませんでした: " + e.message);
    btn.disabled = false; btn.textContent = "インストール";
  }
});

/* ---------------------------------------------------------------- 進捗 (install / cal 兼用) */
function setProgChrome(kicker, title, logOn = true) {
  $("#pg-kicker").textContent = kicker;
  $("#pg-title").textContent = title;
  $("#pg-logbox").style.display = logOn ? "" : "none";
}
function renderProgress() {
  const r = W.job || {};
  const steps = r.steps || [];
  const idx = steps.indexOf(r.step);
  $("#pg-steps").innerHTML = steps.map((name, i) => {
    const cur = r.step === name && (r.status === "running" || r.status === "queued");
    const done = (idx >= 0 && idx > i) || r.status === "done";
    const bad = r.status === "failed" && cur;
    const cls = cur && !bad ? "now" : done ? "ok" : bad ? "bad" : "wait";
    return `<li class="${cls}"><span class="ic">${cur && !bad ? '<span class="spin"></span>' : ""}</span>${esc(stepLabel(name, W.kind))}</li>`;
  }).join("");
  $("#pg-cancel").style.display = (r.status === "running" || r.status === "queued") ? "" : "none";
  $("#pg-log").textContent = W.logText || "(ログ待ち…)";
  const err = $("#pg-err");
  err.classList.toggle("hidden", !r.error);
  if (r.error) err.textContent = r.error;
  $("#pg-title").textContent = W.kind === "install"
    ? (r.phase === "model"
        ? (r.status === "done" ? "モデルの準備が完了しました"
           : r.status === "running" ? `${specName(r.backend) || "Strata"} がモデルを準備しています (時間がかかります)`
           : (STEP_NAMES[r.status] || "準備中"))
        : (r.status === "done" ? "インストールが完了しました" : STEP_NAMES[r.status] && r.status !== "running" ? STEP_NAMES[r.status] : "インストール中…"))
    : $("#pg-title").textContent;
}
async function pullLog(ep) {
  if (!W.job) return;
  try {
    const r = await api(`/${ep}/${W.job.id}/log?cursor=${W.logCursor}`);
    const lines = r.lines || [];
    if (r.start < W.logCursor || r.start === 0) W.logText = lines.join("\n");
    else if (lines.length) W.logText = (W.logText ? W.logText + "\n" : "") + lines.join("\n");
    W.logCursor = (r.start || 0) + lines.length;
    if (curStep === "s-progress" || curStep === "s-download")
      (curStep === "s-download" ? $("#dl-log") : $("#pg-log")).textContent = W.logText || "(ログ待ち…)";
  } catch { /* ログは取れなくても進捗に影響しない */ }
}
async function pollTick() {
  if (!W.job) return;
  const ep = W.kind === "cal" ? "api/calibrations" : "api/installs";
  try {
    const r = await api(`/${ep}/${W.job.id}`);
    W.job = r;
  } catch { return; }
  if (W.kind === "download") { await pullLog("api/installs"); renderDownload(); }
  else {
    renderProgress();
    await pullLog(W.kind === "cal" ? "api/calibrations" : "api/installs");
  }
  const st = W.job.status;
  if (st === "done" || st === "failed" || st === "cancelled" || st === "lost") {
    stopPoll();
    await finishJob();
  }
}
/* インストール完了後の追い書き */
const INSTALL_NOTES = {
  strata: "インストール完了です。次はモデルの設定 — 家系・サイズ・コンテキスト長を選ぶと、"
    + "Strata 本体のセットアップ手順 (ダウンロードと strata-<model>.json 作成) がこの画面のまま"
    + "走りきるため、シェルの準備はありません。",
  maya: "インストール完了です。次はモデルの設定 — 家系 (GLM / DeepSeek / MiniMax / Qwen)・"
    + "サイズ (量子化)・コンテキスト長を選ぶと、Maya 本体のセットアップ手順 (エンジンビルドと"
    + "ダウンロード、maya-<model>.json 作成) がこの画面のまま走りきるため、シェルの準備はありません。",
};
async function finishJob() {
  const r = W.job || {};
  if (W.kind === "install") {
    if (r.phase === "model") {
      // Strata / Maya ② のモデル構成ジョブ: 設定先行フローではプロファイルへの
      // config 紐付け + ③ の回答適用をして調整 (⑤) へ。旧フロー (実行中ジョブの
      // 再開 / 済みモデルの即時採用) は従来どおり稼働設定 (③) へ直行する
      if (r.status === "done") {
        const prefix = r.backend === "maya" ? "maya" : "strata";
        W.summary.push({ ok: true, t: `${specName(r.backend) || "Strata"} のモデル設定が完了しました (${prefix}-<model>.json 作成済み)` });
        if (W.preJob && W.cfgProfId) {
          await bindSetupResult(r);
          renderCal(); go("s-cal"); return;
        }
        renderConfig(); go("s-config"); return;
      }
      renderProgress();
      offerRetry("モデル選びやり直す");
      return;
    }
    if (r.status === "done") {
      W.summary.push({ ok: true, t: `${specName(r.backend)} をインストールしました` });
      $("#ok-title").textContent = `${specName(r.backend)} のインストールが完了しました`;
      $("#ok-sub").textContent = INSTALL_NOTES[r.backend]
        || "次はモデルのダウンロードです。まだモデルが無い場合はダウンロードしてください。";
      go("s-installed");
    } else {
      renderProgress();
      offerRetry("やり直す");   // 失敗 / 中断は確認画面へ戻して再実行できるようにする
    }
  } else if (W.kind === "download") {
    renderDownload();
    if (r.status === "done") {
      W.summary.push({ ok: true, t: `モデル ${r.repo_id} を取得しました` });
      $("#dl-title").textContent = "ダウンロードが完了しました";
      $("#dl-bar").style.width = "100%";
      $("#dl-note").textContent = bytes((r.progress || {}).done) + " を保存しました";
      $("#dl-cancel").style.display = "none";
      // 設定先行フロー: モデルをプロファイルに紐付けて調整 (⑤) へ自動で進む。
      // 旧フローは「次へ」ボタンで稼働設定 (③) へ
      if (W.preJob && W.cfgProfId) {
        await bindDownloadResult(r);
        renderCal(); go("s-cal"); return;
      }
      $("#dl-next").classList.remove("hidden");
    } else if (r.status === "cancelled") {
      $("#dl-title").textContent = "中断しました";
      $("#dl-retry").hidden = false;
      /* 中断後はダウンロード途中のモデルを削除するか選ぶ画面へ (1 ジョブ 1 回) */
      if (!W.cleanupShown) { W.cleanupShown = true; renderCleanup(); go("s-cleanup"); }
    } else {
      $("#dl-err").textContent = r.error || "ダウンロードに失敗しました";
      $("#dl-err").classList.remove("hidden");
      $("#dl-title").textContent = "ダウンロードに失敗しました";
      $("#dl-retry").hidden = false;
    }
  } else if (W.kind === "cal") {
    if (r.status === "done") { W.summary.push({ ok: true, t: "調整 (キャリブレーション) が完了しました" }); renderDone(); go("s-done"); }
    else { renderProgress(); offerRetry("完了画面へ進む"); }
  }
}
/* 失敗 / 中断時に次へボタンを出して行き先を選ぶ (中断→再実行、調整失敗→そのまま完了) */
function offerRetry(label) {
  const b = $("#pg-next");
  b.textContent = label;
  b.classList.remove("hidden");
  b.onclick = () => {
    b.classList.add("hidden");
    if (W.kind === "cal") { W.summary.push({ ok: false, t: "調整は完了しませんでした (後で実行できます)" }); renderDone(); go("s-done"); }
    else if ((W.job || {}).phase === "model") { renderModel(); go("s-model"); }
    else { renderReview(); }
  };
}
$("#pg-cancel").addEventListener("click", async () => {
  if (!W.job) return;
  if (!confirm("実行中のジョブを中断しますか?")) return;
  try {
    await api(`/${W.kind === "cal" ? "api/calibrations" : "api/installs"}/${W.job.id}/cancel`, { method: "POST" });
  } catch (e) { alert("中断できませんでした: " + e.message); }
});
$("#dl-retry").addEventListener("click", () => { renderModel(); go("s-model"); });
$("#dl-next").addEventListener("click", () => { renderConfig(); go("s-config"); });

/* ---------------------------------------------------------------- 2: モデル
   2択のプルダウン: 「ダウンロードする」= supported_models 表から / 「済みのを使う」
   = /api/hf-models (HF キャッシュ + 各バックエンドのモデルフォルダ) から選ぶ。
   repo id 直接入力は「その他」の details に格納し、既定の導線にはしない。 */
let mdLocal = [];                                   // /api/hf-models のダウンロード済み一覧
const mdMode = () => ($$("#md-modes input").find(i => i.checked) || {}).value || "dl";
/* ---- Strata / Maya: 純正セットアップを --yes (非対話) で走らせる選択面板 --------
   面板 (家系/サイズ/コンテキスト/ビジョン) は共通。表の単一の源は submodule で、
   Strata は strata_setup / Maya は maya_setup が読み出す (mdT() が現在バックエンドの表)。 */
const MD_LABELS = {
  other: [["モデルをダウンロードする", "対応モデルの一覧から選んで取得します"],
          ["ダウンロード済みのモデルを使う", "この PC にあるモデルから選びます (再取得しません)"]],
  strata: [["モデルをダウンロードする", "家系・サイズ・コンテキスト長を選ぶと、Strata 本体のセットアップ手順でダウンロードと設定作成まで走ります"],
           ["ダウンロード済みのモデルを使う", "この PC に重みが在るモデルから選びます (再取得しません)"]],
  maya: [["モデルをダウンロードする", "家系・サイズ (量子化)・コンテキスト長を選ぶと、Maya 本体のセットアップ手順でエンジンビルド・ダウンロード・設定作成まで走ります"],
         ["ダウンロード済みのモデルを使う", "この PC に重みが在るモデルから選びます (再取得しません)"]],
};
const mdTableBackend = () => W.backend === "maya" || W.backend === "strata";
function setMdModeLabels(backend) {
  const L = MD_LABELS[backend] || MD_LABELS.other;
  $("#md-dl-mode-t1").textContent = L[0][0]; $("#md-dl-mode-t2").textContent = L[0][1];
  $("#md-local-mode-t1").textContent = L[1][0]; $("#md-local-mode-t2").textContent = L[1][1];
  // Strata / Maya はモデル設定なしでは起動設定 (values.config) が作れないので「あとでする」を隠す。
  // profile モードはモデル選択が必須 (調整にモデルが要る) ので常に隠す
  $("#md-later").classList.toggle("hidden", profileFlow() || backend === "strata" || backend === "maya");
}
async function loadStrata() {
  // profile モードは setup-options が読んだ表を使う (/api/installs/* は install 権限)
  if (profileFlow()) return (W.options && W.options.strata)
    || { available: false, families: {}, contexts: [], configs: [], venv_ready: false };
  if (W.mdStrata) return W.mdStrata;
  try { W.mdStrata = await api("/api/installs/strata-model"); }
  catch { W.mdStrata = { available: false, families: {}, contexts: [], configs: [], venv_ready: false }; }
  return W.mdStrata;
}
async function loadMaya() {
  if (profileFlow()) return (W.options && W.options.maya)
    || { available: false, families: {}, configs: [], venv_ready: false };
  if (W.mdMaya) return W.mdMaya;
  try { W.mdMaya = await api("/api/installs/maya-model"); }
  catch { W.mdMaya = { available: false, families: {}, configs: [], venv_ready: false }; }
  return W.mdMaya;
}
/* 現在バックエンドの表 (Strata / Maya の面板は同じ描画関数を使う) */
const mdT = () => (W.backend === "maya" ? W.mdMaya : W.mdStrata) || {};
function mdStModels() {
  return ((mdT().families || {})[W.mdStFam] || {}).models || {};
}
function mdStFull(m) {
  const h = (mdStModels()[m] || {}).have || {};
  return !!(h.files && h.found >= h.files);
}
function mdStFillSizes() {
  const st = mdT();
  const ms = mdStModels();
  const isMaya = W.backend === "maya";
  const ram = (st.pc || {}).ram_gb || 0;
  const ids = Object.keys(ms);
  // 推奨: Strata = この PC の RAM に収まる最大サイズ (無ければ最小)。
  // Maya = 純正の選び方そのまま (表の rec: GLM は 24GB 以下で Maya-S24、DS4 は公開済み
  // Maya quant 優先、MM3 は VRAM+RAM に収まるもの、Qwen は Q5_K_M)
  let rec;
  if (isMaya) {
    rec = ((st.families || {})[W.mdStFam] || {}).rec || ids[0] || "";
  } else {
    const fit = ids.filter(m => !(Number(ms[m].ram_gb) > ram))
                   .sort((a, b) => (ms[b].ram_gb || 0) - (ms[a].ram_gb || 0));
    rec = fit[0] || ids[0] || "";
  }
  // ダウンロード択の一覧は「まだ無いモデル」から選ぶ — 済み / 未公開のものはグレー表示 (選択不可)
  const avail = m => !mdStFull(m) && ms[m].published !== false;
  if (!ids.includes(W.mdStModel) || !avail(W.mdStModel)) {
    W.mdStModel = (rec && avail(rec) ? rec : "") || ids.find(avail) || rec || "";
  }
  $("#md-size").innerHTML = ids.map(m => {
    const d = ms[m] || {}; const bits = [];
    if (isMaya) {
      if (d.memory_gb) bits.push(`VRAM+RAM 約${Math.round(d.memory_gb)} GB`);
    } else if (d.ram_gb) {
      bits.push(`RAM ${d.ram_gb} GB${ram && d.ram_gb > ram ? " ※この PC " + ram + " GB" : ""}`);
    }
    if (!mdStFull(m) && d.download_gb) bits.push(`DL ${isMaya ? Math.round(d.download_gb) : d.download_gb} GB`);
    const unpub = d.published === false;
    const label = (d.label || m)
      + (mdStFull(m) ? " (ダウンロード済み" + (((d.have || {}).gb) ? " " + d.have.gb + " GB" : "") + ")" : "")
      + (unpub ? " (未公開)" : "")
      + (bits.length ? " — " + bits.join(" / ") : "")
      + (m === rec && avail(m) ? " (この PC 推奨)" : "");
    return `<option value="${esc(m)}"${m === W.mdStModel ? " selected" : ""}${mdStFull(m) || unpub ? " disabled" : ""}>`
      + `${esc(label)}</option>`;
  }).join("") || '<option value="">(サイズ一覧が空です)</option>';
  // vision は対応パックだけ選択を出して既定 ON。非対応は送信しない
  const d = ms[W.mdStModel] || {};
  $("#md-vision-opt").style.display = d.vision ? "" : "none";
  $("#md-vision-opt").classList.toggle("sel", $("#md-vision").checked);
  mdStHint();
}
function mdStRecCtx() {
  const d = mdStModels()[W.mdStModel] || {};
  return Number(d.rec_context) || (W.backend === "maya" ? 32768 : 131072);
}
/* GPU の使い方 (複数枚共有) は ③ 稼働設定の GPU 分割質問 (FreeToken 基準の tp 位置) で聞く。
   ② はモデル構成 (家系/サイズ/コンテキスト/ビジョン) だけを担当する。 */
function mdStFillCtx() {
  const st = mdT();
  const fam = (st.families || {})[W.mdStFam] || {};
  const maxc = Number(fam.max_context) || 524288;
  // コンテキスト一覧は家系が持つ (Maya: GLM 128K / DS4・MM3 1M / Qwen 256K が上限)
  const src = (fam.contexts || []).length ? fam.contexts : (st.contexts || []);
  const cs = (src.length ? src : [8192, 32768, 65536, 131072, 204800, 262144])
    .filter(c => c <= maxc);
  // 既定は純正の推奨 (own_path_context / ram_ctx / recommended_context 系)。家族・サイズを変えたら推奨へ戻る
  const rec = mdStRecCtx();
  if (!cs.includes(W.mdStCtx) || W.mdStCtxRec !== rec)
    W.mdStCtx = cs.includes(rec) ? rec : (cs.includes(131072) ? 131072 : cs[0]);
  W.mdStCtxRec = rec;
  $("#md-ctx").innerHTML = cs.map(c => `<option value="${c}"${c === W.mdStCtx ? " selected" : ""}>`
    + `${c >= 1024 ? Math.round(c / 1024) + "K" : c}${c === rec ? " (推奨)" : c > rec ? " (大)" : ""}</option>`).join("");
  mdStHint();   // 推奨と選択の差はヒントに出す (サイズ変更後は ctx が後から変わるためここで再計算)
}
function mdStHint() {
  const st = mdT();
  const d = mdStModels()[W.mdStModel] || {};
  const fam = (st.families || {})[W.mdStFam] || {};
  const isMaya = W.backend === "maya";
  const name = isMaya ? "Maya" : "Strata";
  const pc = st.pc || {}; const parts = [];
  if (!st.available) parts.push(`${name} のモデル一覧を読み出せませんでした (PC が高負荷の可能性) — 少し待ってからもう一度お試しください`);
  else if (!st.venv_ready) parts.push(`${name} の実行環境 (.venv) がまだありません — 前に戻って ① インストールを実行してください`);
  if (d.about) parts.push(d.about);
  const h = d.have || {};
  if (h.files && h.found >= h.files) {
    parts.push(`重みはこの PC に在ります — 純正セットアップが検証してダウンロードを素通りし、`
      + "config JSON の作成だけ走るので速いです");
  } else if (h.found) {
    parts.push(`重みが部分的です (${h.found}/${h.files}) — 足りない分は受信します`);
  } else if (d.download_gb) {
    parts.push(`ダウンロード ${isMaya ? Math.round(d.download_gb) : d.download_gb} GB`);
  }
  if (isMaya) {
    if (d.memory_gb) parts.push(`VRAM+RAM 約${Math.round(d.memory_gb)} GB で全体を保持 (足りない分は SSD から読みます)`);
    if (d.published === false) parts.push("この量子化はまだ公開されていません — 公開まで選べません");
    if (d.gated) parts.push("ゲートリポジトリです: Hugging Face のトークン (HF_TOKEN / hf auth login) が必要です");
    if (st.available && fam.engine_ready === false)
      parts.push("エンジンはこの設定の中でこの PC 向けにコンパイルします (初回 10〜30 分)");
    if (st.available && W.mdStFam !== "glm")
      parts.push("drafter (DSpark / MTP) の速度計測はセットアップでは行わず、⑤ の調整に集約します (スキップすると drafter なしで起動)");
    if (st.available && W.mdStFam === "minimax")
      parts.push("MiniMax は純正仕様どおり短い prefill 計測 (数分・エンジン起動 1 回) だけセットアップに含まれます");
  } else if (d.ram_gb) {
    parts.push(`RAM ${d.ram_gb} GB 推奨`);
  }
  if (pc.disk_free_gb) parts.push(`モデル保存先 (${pc.data_dir || (isMaya ? "Maya-data" : "Strata-data")}) の空き ${pc.disk_free_gb} GB`);
  /* 純正の対話と同じ立場: 推奨より大きいコンテキストは「注記して従う」。
     24GB クラスで 512K はエンジン起動時の KV 確保に失敗しうる (2026-11-09 実測) */
  const rec = mdStRecCtx();
  if (W.mdStCtx > rec) {
    parts.push(`※ コンテキスト ${Math.round(W.mdStCtx / 1024)}K はこの PC 向け推奨 ${Math.round(rec / 1024)}K より大きい — 起動時に VRAM/RAM 不足で失敗することがあります`);
  }
  /* own-path 家系 (Qwen3.6) は純正が単一 GPU 宣言 — 2 枚在っても分割できない */
  const cards = ((st.pc || {}).gpus || []).filter(g => g.usable);
  if (cards.length >= 2 && fam.multi_gpu === false)
    parts.push("この家系は現在単一 GPU 専用です (純正: runs on one GPU so far)");
  $("#md-strata-hint").textContent = parts.join(" ・ ");
}
function mdSync() {
  const mode = mdMode();
  $$("#md-modes .opt").forEach(o => {
    const i = o.querySelector("input"); o.classList.toggle("sel", i.checked); });
  if (mdTableBackend()) {
    /* 2 択は他バックエンドと同じ形: ダウンロード択 (済みのサイズはグレー) /
       ダウンロード済みのモデルを使う (モデル名で並べ、JSON は一切見せない) */
    const isMaya = W.backend === "maya";
    $("#md-adv").hidden = true;
    $("#md-repo").value = "";                        // repo id 直接入力は純正セットアップでは意味が無い
    ["#md-dl-field", "#md-local-field", "#md-speed-field"].forEach(s => $(s).classList.add("hidden"));
    $("#md-strata").classList.toggle("hidden", mode !== "dl");
    $("#md-strata-exist-field").classList.toggle("hidden", mode !== "local");
    const btn = $("#md-start");
    const st = mdT();
    if (mode === "dl") {
      btn.textContent = "次へ (稼働設定)";
      const sizeOpt = $("#md-size").selectedOptions[0];
      btn.disabled = !st.available || !st.venv_ready || !W.mdStFam || !W.mdStModel
        || !!(sizeOpt && sizeOpt.disabled);   // 取得済み (グレー) の選択では開始しない
      $("#md-dest").textContent = isMaya
        ? "ダウンロードと設定は Maya 本体の手順が走ります — エンジンビルド (初回) と pack 作成も含みます。中断しても続きから再開できます"
        : "ダウンロードと設定は Strata 本体の手順が走ります — 中断しても続きから再開できます";
    } else {
      btn.textContent = "このモデルで続ける";
      btn.disabled = !$("#md-strata-exist").value;
      const sel = (st.downloaded || []).find(x => x.key === $("#md-strata-exist").value);
      $("#md-dest").textContent = sel
        ? `重みはこの PC に在ります (${sel.gb || "?"} GB) — 再取得はありません`
        : "";
    }
    return;
  }
  setMdModeLabels(W.backend);
  $$("#md-modes .opt").forEach(o => {
    if ((o.querySelector("input") || {}).value === "local") o.style.display = "";
  });
  const adv = $("#md-adv");
  adv.hidden = mode !== "dl";
  if (mode !== "dl") $("#md-repo").value = "";       // 直接入力はダウンロード時のみ意味を持つ
  $("#md-strata").classList.add("hidden");
  $("#md-strata-exist-field").classList.add("hidden");
  $("#md-dl-field").classList.toggle("hidden", mode !== "dl");
  $("#md-local-field").classList.toggle("hidden", mode !== "local");
  $("#md-speed-field").classList.toggle("hidden", mode !== "dl");
  const btn = $("#md-start");
  if (mode === "dl") {
    btn.textContent = "次へ (稼働設定)";
    const repo = $("#md-repo").value.trim();
    const selOpt = $("#md-sel").selectedOptions[0];
    // repo id 手入力は明示 intent なので許可。一覧選択は取得済み (グレー) を弾く
    btn.disabled = repo ? false : (!$("#md-sel").value || !!(selOpt && selOpt.disabled));
    const sp = spec();
    $("#md-dest").textContent = sp && sp.model_data
      ? `保存先: ${sp.name} のモデルフォルダ (起動設定に自動で反映されます)`
      : `保存先: HF キャッシュ (${W.installs.cacheDir || "既定"})`;
  } else {
    btn.textContent = "このモデルを使う";
    btn.disabled = !$("#md-local").value;
    const m = mdLocal.find(x => x.repo_id === $("#md-local").value);
    $("#md-dest").textContent = m ? `場所: ${m.snapshot}` : "";
  }
}
/* profile モード: ダウンロード択は出さない — 「ダウンロード済みのモデルを使う」に固定 */
function mdForceLocal() {
  if (!profileFlow()) return;
  const localR = $$("#md-modes input").find(i => i.value === "local");
  if (localR) localR.checked = true;
  $("#md-modes").classList.add("hidden");
  $("#md-later").classList.add("hidden");   // モデル無し完了はこのフローの目的外 (調整にモデルが要る)
}
async function renderModel() {
  mdForceLocal();
  if (W.backend === "strata") { await renderModelStrata(); return; }
  if (W.backend === "maya") { await renderModelMaya(); return; }
  setMdModeLabels(W.backend);
  const sp = spec();
  const ms = (sp && sp.supported_models) || [];
  W.mdList = ms;                                    // クリック時に include (取得ファイル) を引く
  try {
    const hm = await api("/api/hf-models");
    /* 選択中バックエンドが読める形のモデルだけに出す
       (basalt = .basalt pack / llama.cpp = gguf / 他は汎用重みディレクトリ)。
       has_weights は .basalt を含まない (API 側で basalt 専用 pack と分離済み) なので、
       basalt pack だけが中身の repo は FreeToken 等の一覧には出ない */
    const shape = W.backend === "basalt" ? "basalt" : (W.backend === "llamacpp" ? "gguf" : "");
    mdLocal = (hm.models || []).filter(m => m.repo_id && (
      shape === "basalt" ? (m.basalt || []).length
      : shape === "gguf" ? ((m.gguf || []).length || m.has_weights)
      : m.has_weights));
  } catch { mdLocal = []; }
  /* ダウンロード択の一覧: 取得済みは選択不可 (グレー) — 済みモデルは
     「ダウンロード済みのモデルを使う」側で選ぶ (再ダウンロードの誤起動防止) */
  const haveRepo = r => mdLocal.some(x => x.repo_id === r);
  const firstAvail = ms.findIndex(m => !haveRepo(m.repo_id));
  $("#md-sel").innerHTML = ms.length
    ? ms.map((m, i) => {
        const got = haveRepo(m.repo_id);
        return `<option value="${esc(m.repo_id)}"${i === (firstAvail >= 0 ? firstAvail : 0) ? " selected" : ""}${got ? " disabled" : ""}>`
          + `${esc(m.repo_id)}${m.name ? " — " + esc(m.name) : ""}${got ? " (ダウンロード済み)" : ""}</option>`;
      }).join("")
    : '<option value="">(このバックエンドに対応モデル一覧がありません)</option>';
  $("#md-local").innerHTML = mdLocal.length
    ? mdLocal.map((m, i) => `<option value="${esc(m.repo_id)}"${i === 0 ? " selected" : ""}>`
        + `${esc(m.repo_id)}`
        + (W.backend === "basalt" && (m.basalt || []).length ? ` · ${esc(m.basalt[0].name)}` : "")
        + `${m.source === "data" ? " — 導入済みモデルフォルダ" : ""}</option>`).join("")
    : '<option value="">(このバックエンドで使えるダウンロード済みモデルが見つかりません)</option>';
  /* 済みモデルが 0 件ならその選択を不可にしてダウンロード択に寄せる
     (profile モードはダウンロード択が無いので切り替えない — 開始ボタンだけ無効) */
  const localR = $$("#md-modes input").find(i => i.value === "local");
  localR.disabled = !mdLocal.length;
  $$("#md-modes .opt").forEach(o => { if (o.contains(localR)) o.style.opacity = mdLocal.length ? "" : ".45"; });
  if (localR.disabled && mdMode() === "local" && !profileFlow()) {
    $$("#md-modes input").find(i => i.value === "dl").checked = true;
  }
  $("#md-err").classList.add("hidden");
  $("#md-sub").textContent = profileFlow()
    ? "この PC にダウンロード済みのモデルから選んでください。新しいダウンロードは行いません。"
    : (ms.length
      ? "新しいモデルをダウンロードするか、この PC ですでにダウンロード済みのモデルを使うかを選んでください。"
      : "このバックエンドには対応モデル一覧がないため、「その他: repo id を直接入力する」から選ぶか、ダウンロード済みのモデルを使ってください。");
  mdSync();
  if (profileFlow() && !mdLocal.length)
    $("#md-dest").textContent = "ダウンロード済みのモデルが見つかりません。先に通常のセットアップでモデルをダウンロードしてください。";
}
$$("#md-modes input").forEach(i => i.addEventListener("change", mdSync));
$("#md-sel").addEventListener("change", mdSync);
$("#md-local").addEventListener("change", mdSync);
$("#md-repo").addEventListener("input", mdSync);
/* Strata 面板の選択変更 */
$("#md-family").addEventListener("change", () => {
  W.mdStFam = $("#md-family").value; W.mdStModel = ""; mdStFillSizes(); mdSync(); });
$("#md-size").addEventListener("change", () => { W.mdStModel = $("#md-size").value; mdStFillSizes(); mdStFillCtx(); mdSync(); });
$("#md-ctx").addEventListener("change", () => { W.mdStCtx = parseInt($("#md-ctx").value, 10); mdStHint(); mdSync(); });
$("#md-vision").addEventListener("change", () => {
  $("#md-vision-opt").classList.toggle("sel", $("#md-vision").checked); mdSync(); });
$("#md-strata-exist").addEventListener("change", mdSync);
async function renderModelStrata() { await renderModelTable("strata"); }
async function renderModelMaya() { await renderModelTable("maya"); }
/* Strata / Maya 共通の ② 描画: 家系 → サイズ (量子化) → コンテキスト → ビョン。
   表の単一の源は submodule (API 経由)。JSON 名は内部だけで UI にはモデル名を出す。 */
async function renderModelTable(backend) {
  mdForceLocal();
  setMdModeLabels(backend);
  $("#md-vision-t2").textContent = backend === "maya"
    ? "ON のままなら Maya がこの PC に合う読み方 (エンジン内蔵 / 別エンコーダ) を選びます。オフにすると画像リクエストは使えません (--no-vision)"
    : "ON のままなら Strata がこの PC に合う読み方 (VRAM に応じて GPU/CPU エンコーダ) を選びます。オフにすると画像リクエストは使えません";
  // 再入時は表も設定済み一覧も最新の取り直し (サーバー側に 60s キャッシュ)
  if (backend === "maya") W.mdMaya = null; else W.mdStrata = null;
  const st = await (backend === "maya" ? loadMaya() : loadStrata());
  const isMaya = backend === "maya";
  const fams = st.families || {};
  const famIds = Object.keys(fams).filter(f => Object.keys((fams[f] || {}).models || {}).length);
  const defFam = isMaya ? "glm" : "qwen";
  if (!famIds.includes(W.mdStFam)) W.mdStFam = famIds.includes(defFam) ? defFam : (famIds[0] || "");
  $("#md-family").innerHTML = famIds.map(f => `<option value="${esc(f)}"${f === W.mdStFam ? " selected" : ""}>`
    + `${esc(fams[f].title || f)}</option>`).join("")
    || `<option value="">(${isMaya ? "Maya" : "Strata"} のモデル一覧が読めません)</option>`;
  mdStFillSizes(); mdStFillCtx();
  /* ダウンロード済み一覧: 表から作る (モデル名 + サイズ + GB)。JSON 名は内部では
     使うが UI には出さない — 「そのモデルが在る」だけがユーザーの情報。
     profile モードは config JSON 作成済みのモデルだけ (セットアップジョブを
     走らせないフローなので、重みが在っても config が無いモデルは選べない) */
  const dl = [];
  for (const fid of famIds) {
    const f = fams[fid];
    for (const [m, d] of Object.entries(f.models || {})) {
      if (mdStFullIn(f, m)) dl.push({ key: fid + "/" + m, fam: fid, model: m,
                                      label: (f.title || fid) + " · " + (d.label || m),
                                      gb: (d.have || {}).gb });
    }
  }
  const ready = profileFlow()
    ? dl.filter(x => (st.configs || []).some(y => y.name === mdCfgKey(fams[x.fam] || {}, x.model)
        && (y.weights || {}).ok === true && (!y.quant_key || y.quant_key === x.model)))
    : dl;
  st.downloaded = ready;
  $("#md-strata-exist").innerHTML = ready.map((x, i) => `<option value="${esc(x.key)}"${i === 0 ? " selected" : ""}>`
    + `${esc(x.label + " — " + (x.gb || "?") + " GB")}</option>`).join("")
    || (profileFlow()
      ? '<option value="">(設定作成済みのモデルがありません — 先に通常のセットアップでモデル設定を実行してください)</option>'
      : '<option value="">(ダウンロード済みのモデルがありません)</option>');
  const localR = $$("#md-modes input").find(i => i.value === "local");
  localR.disabled = !ready.length;
  $$("#md-modes .opt").forEach(o => { if (o.contains(localR)) o.style.opacity = ready.length ? "" : ".45"; });
  if (localR.disabled && mdMode() === "local" && !profileFlow()) {
    $$("#md-modes input").find(i => i.value === "dl").checked = true;
  }
  $("#md-err").classList.add("hidden");
  $("#md-sub").textContent = profileFlow()
    ? (isMaya
      ? "この PC にダウンロード済みで設定 (maya-<model>.json) 作成済みの Maya モデルから選んでください。新しいダウンロード / エンジンビルドは行いません。"
      : "この PC にダウンロード済みで設定 (strata-<model>.json) 作成済みの Strata モデルから選んでください。新しいダウンロードは行いません。")
    : (isMaya
    ? "Maya は家系 (GLM / DeepSeek / MiniMax / Qwen)・サイズ (量子化)・コンテキスト長を選ぶだけです。"
      + "Maya 本体のセットアップ手順がこのまま走り、エンジンビルド (初回)・ダウンロードから起動設定 "
      + "(maya-<model>.json) の作成まで完了します。ダウンロード済みのモデルは"
      + "「ダウンロード済みのモデルを使う」から再取得なしで選べます。"
    : "Strata は家系・サイズ・コンテキスト長を選ぶだけです。Strata 本体のセットアップ手順がこのまま走り、"
      + "ダウンロードから起動設定 (strata-<model>.json) の作成まで完了します。ダウンロード済みのモデルは"
      + "「ダウンロード済みのモデルを使う」から再取得なしで選べます。");
  mdSync();
}
function mdStFullIn(f, m) {
  const h = ((f || {}).models || {})[m] || {};
  const hv = h.have || {};
  return !!(hv.files && hv.found >= hv.files);
}
/* 純正命名作法の config 名: Strata = strata-<tag+model>.json / Maya = 表の config
   (maya-<quant>.json / maya-ds4.json / maya-mm3.json / maya-cyber-frost.json ...) */
function mdCfgKey(f, m) {
  return W.backend === "maya"
    ? String(((f.models || {})[m] || {}).config || `maya-${m.toLowerCase()}.json`)
    : "strata-" + String((f.tag || "") + m).toLowerCase() + ".json";
}
/* 設定先行フローの ② 確定: 選択を W に控えて ③ へ。ジョブの POST は ④ の後
   (startModelJob) — DL / セットアップの前に設定をまとめて終える */
function mdCaptureTable() {
  const st = mdT();
  if (!st.available || !st.venv_ready || !W.mdStFam || !W.mdStModel) return false;
  /* vision ON = フラグを渡さない = 純正のマシン既定。OFF = 明示の "no" (Strata) /
     "off" (Maya --no-vision)。Strata own-path 系は VRAM が 24GB 未満なら CPU
     エンコーダ、以上で GPU エンコーダ (2026-11-09: 24.4GB 未満のこの PCで
     --vision gpu を送るのは推奨外) */
  const off = W.backend === "maya" ? "off" : "no";
  const vision = ($("#md-vision-opt").style.display === "none") ? ""
               : ($("#md-vision").checked ? "" : off);
  W.tableReq = { family: W.mdStFam, model: W.mdStModel,
                 context: parseInt($("#md-ctx").value, 10) || (W.backend === "maya" ? 32768 : 131072),
                 vision };
  W.dlReq = null;
  return true;
}
function mdEnterSettings() {
  W.preJob = true; W.modelSkipped = false;
  W.pendingUpdates = null; W.setupGpus = ""; W.setupParallel = 0;
  renderConfig(); go("s-config");
}
/* Strata / Maya の「ダウンロード済みのモデルを使う」: config が在って重み ok なら
   即時採用 (ジョブなし = 待ち時間なしなので従来の ③ 順序)。重みが在るのに config が
   無い (削除/別経路) ときは純正セットアップジョブで検証・config 作成だけ走らせる
   (ダウンロードは起きない) — こちらは設定先行フローに乗せる */
async function mdAdoptLocalTable() {
  const st = mdT();
  const x = (st.downloaded || []).find(v => v.key === $("#md-strata-exist").value);
  if (!x) return;
  const f = (st.families || {})[x.fam] || {};
  const key = mdCfgKey(f, x.model);
  /* Maya の DS4 / MM3 は量子化が違っても config 名を共有する — installer 記録の
     量子化キー (quant_key) でも突き合わせ、別量子化の config を掴まない */
  const c = (st.configs || []).find(y => y.name === key && (y.weights || {}).ok === true
    && (!y.quant_key || y.quant_key === x.model));
  if (c) {
    W.kind = "install";
    W.job = { id: "", kind: "install", phase: "model", status: "done", backend: W.backend,
              repo_id: "", config_name: c.name, model: x.fam + "/" + x.model,
              result: { config: c.file, parallel: c.parallel || null } };
    W.preJob = false; W.modelSkipped = false;
    W.cfgProfId = null; W.cfgProfNew = false;
    W.summary.push({ ok: true, t: `ダウンロード済みの ${x.label} を使用します` });
    renderConfig(); go("s-config");
    return;
  }
  /* profile モードはセットアップジョブ (config 作成) を走らせない — 一覧は
     config 作成済みだけで作るので通常ここには来ない。万一の取りこぼしは正直に止める */
  if (profileFlow()) {
    $("#md-err").textContent = "このモデルの config JSON が見つかりません。先に通常のセットアップでモデル設定を実行してください。";
    $("#md-err").classList.remove("hidden");
    return;
  }
  const d = (f.models || {})[x.model] || {};
  W.tableReq = { family: x.fam, model: x.model,
                 context: d.rec_context || (W.backend === "maya" ? 32768 : 131072), vision: "" };
  W.dlReq = null;
  mdEnterSettings();
}
$("#md-start").addEventListener("click", async () => {
  $("#md-err").classList.add("hidden");
  if (mdMode() === "local") {
    if (mdTableBackend()) { await mdAdoptLocalTable(); return; }
    /* 済みモデルは「完了済みのダウンロードジョブ」に化かして後続 (稼働設定の
       プロファイル名 / model_path 解決) をそのまま通す。待ち時間が無いので
       設定は従来どおりジョブ後 (③) に聞く */
    const m = mdLocal.find(x => x.repo_id === $("#md-local").value);
    if (!m) return;
    W.kind = "download";
    W.job = { id: "", kind: "download", status: "done", backend: W.backend,
              repo_id: m.repo_id, result: { path: m.snapshot }, local_dir: m.snapshot };
    W.preJob = false; W.modelSkipped = false;
    W.cfgProfId = null; W.cfgProfNew = false;
    W.summary.push({ ok: true, t: `ダウンロード済みのモデル ${m.repo_id} を使用します` });
    renderConfig(); go("s-config");
    return;
  }
  /* ダウンロード択: 設定 (③④) を先に集めてからジョブを開始する */
  if (mdTableBackend()) {
    if (!mdCaptureTable()) return;
  } else {
    const repo = $("#md-repo").value.trim() || $("#md-sel").value;
    if (!repo) return;
    /* 一覧から選んだモデルは既定ファイル (pack 1 個) だけ取得する。repo 全体 (1 TB) を
       落とさないための allow_patterns。repo id を手入力したときは指定なし = 全体 */
    const entry = (W.mdList || []).find(m => m.repo_id === repo) || null;
    const sp = spec();
    W.dlReq = { repo_id: repo, revision: "", backend: (sp && sp.model_data) ? sp.id : "",
                allow_patterns: (entry && entry.include) || [],
                max_kbps: parseInt($("#md-speed").value, 10) || 0 };
    W.tableReq = null;
  }
  mdEnterSettings();
});
/* 「あとでする」: モデルなしで設定だけ完了する (プロファイルのモデルは後で編集) */
$("#md-later").addEventListener("click", () => {
  W.modelSkipped = true; W.preJob = true;
  W.dlReq = null; W.tableReq = null;
  W.pendingUpdates = null; W.setupGpus = ""; W.setupParallel = 0;
});
function renderDownload() {
  const r = W.job || {};
  const pr = r.progress || {};
  const t = Date.now() / 1000;
  const done = pr.done || 0;
  /* 速度はポーリング差分からクライアント側で移動平均 (サーバーの瞬時値はバースト転送で乱高下する) */
  if (W.tLast && t - W.tLast > 0.5) {
    if (done >= W.dLast) {
      const v = (done - W.dLast) / (t - W.tLast);
      W.ema = W.ema == null ? v : W.ema * 0.55 + v * 0.45;
    } else {
      W.ema = null;  // ジョブ再実行でカウンタが戻ったら買い直し
    }
  }
  if (done > W.dLast) W.growT = t;
  W.tLast = t; W.dLast = done;
  const running = r.status === "running" || r.status === "queued";
  const bad = r.status === "failed" || r.status === "cancelled" || r.status === "lost";
  $("#dl-title").textContent = r.status === "done" ? "ダウンロードが完了しました"
    : (running || !STEP_NAMES[r.status]) ? "ダウンロード中…" : STEP_NAMES[r.status];
  $("#dl-sub").textContent = r.repo_id || "";
  const pct = pr.file_total ? Math.min(100, 100 * done / pr.file_total) : 0;
  $("#dl-bar").style.width = pct.toFixed(1) + "%";
  let note = `受信 ${bytes(done)}${pr.file_total ? ` / 全体 ${bytes(pr.file_total)}` : ""}`;
  if (running && W.ema != null && W.ema > 0) {
    note += ` · ${bytes(W.ema)}/s`;
    if (pr.file_total > done && W.ema > 4096) {
      const eta = (pr.file_total - done) / W.ema;
      note += ` · 残り約 ${eta >= 60 ? Math.ceil(eta / 60) + " 分" : Math.max(1, Math.round(eta)) + " 秒"}`;
    }
  }
  const f = pr.file || "";
  if (f && f !== "Downloading bytes" && !/^(Reconstructing|Downloading bucket)/.test(f)) note += ` · ${f}`;
  $("#dl-note").textContent = note;
  /* 停滞検知: 3分以上バイトが増えていない場合は正直に警告する */
  const err = $("#dl-err");
  if (running && W.growT && t - W.growT > 180) {
    err.textContent = "⚠ 3分以上バイトが増えていません。接続が停滞している可能性があります。"
      + "「中断」してからもう一度ダウンロードすると、続きから再開できます。";
    err.classList.remove("hidden");
  } else if (!bad) err.classList.add("hidden");
  $("#dl-log").textContent = W.logText || "(ログ待ち…)";
  const canceling = running && !!W.cancelAt;
  const cbtn = $("#dl-cancel");
  cbtn.style.display = running ? "" : "none";
  cbtn.disabled = canceling;
  cbtn.textContent = canceling ? "停止中…" : "中断";
  /* 中断要求から 8 秒経っても process が死なない場合は強制停止 (SIGKILL) を出せるようにする */
  const force = canceling && (Date.now() - W.cancelAt > 8000);
  $("#dl-force").classList.toggle("hidden", !force);
  $("#dl-retry").hidden = !bad;
  $("#dl-next").classList.toggle("hidden", r.status !== "done");
}
$("#dl-cancel").addEventListener("click", async () => {
  if (!W.job || W.cancelAt) return;
  if (!confirm("ダウンロードを中断しますか?\n転送プロセスを停止し、途中ファイルを掃除する確認に進みます。")) return;
  try {
    await api(`/api/installs/${W.job.id}/cancel`, { method: "POST" });
    W.cancelAt = Date.now();
  } catch (e) { alert("中断できませんでした: " + e.message); }
});
$("#dl-force").addEventListener("click", async () => {
  if (!W.job) return;
  if (!confirm("それでも停止しません。強制停止 (SIGKILL) を実行しますか?\n転送中のデータは途中で止まります (破損ファイルは次の掃除画面で削除できます)。")) return;
  $("#dl-force").disabled = true;
  $("#dl-force").textContent = "強制停止中…";
  try { await api(`/api/installs/${W.job.id}/cancel?force=true`, { method: "POST" }); }
  catch (e) { alert("強制停止できませんでした: " + e.message); }
  $("#dl-force").disabled = false;
  $("#dl-force").textContent = "強制停止";
});

/* ------------------------------------------------- 2b: 中断残骸の掃除
   ダウンロードを中断すると、受信途中のファイルが保存先に残る。
   GET /api/installs/{id}/partial で存在・サイズを見て、DELETE で削除する。
   既存モデルへの上書きダウンロードだった場合は .incomplete のOnly削除。 */
let clInfo = null;
async function renderCleanup() {
  const id = (W.job || {}).id || "";
  clInfo = null;
  $("#cl-err").classList.add("hidden");
  $("#cl-box").classList.add("hidden");
  $("#cl-none").classList.add("hidden");
  $("#cl-del").disabled = false; $("#cl-del").textContent = "削除する";
  $("#cl-keep").disabled = false; $("#cl-keep").textContent = "残す";
  $("#cl-sub").textContent = "中断で受信途中のデータが保存先に残っています。"
    + "次に同じモデルを取り直すなら削除しておくと、容量を無駄に使いません。";
  const delBtn = $("#cl-del");
  delBtn.onclick = clDelete;
  $("#cl-keep").onclick = () => { renderModel(); go("s-model"); };
  if (!id) { $("#cl-none").classList.remove("hidden"); return; }
  try {
    clInfo = await api(`/api/installs/${id}/partial`);
  } catch { clInfo = null; }
  if (!clInfo || !clInfo.exists || !clInfo.bytes) {
    $("#cl-none").textContent = "削除対象は見つかりませんでした。そのままで問題ありません。";
    $("#cl-none").classList.remove("hidden");
    $("#cl-del").classList.add("hidden");
    $("#cl-keep").textContent = "モデル選びに戻る";
    return;
  }
  $("#cl-del").classList.remove("hidden");
  $("#cl-box").innerHTML =
      `<div class="row-item"><span class="k">場所</span>`
    + `<span class="v"><span class="mono">${esc(clInfo.path)}</span></span></div>`
    + `<div class="row-item"><span class="k">サイズ</span>`
    + `<span class="v">${bytes(clInfo.bytes)} (${clInfo.files} ファイル${clInfo.partials ? ` · 未完 ${clInfo.partials} 件` : ""})</span></div>`
    + (clInfo.pre_existing ? `<div class="row-item"><span class="k">注意</span>`
        + `<span class="v">既存のモデルがある場所です。削除されるのは未完 (.incomplete) のファイルだけです。</span></div>` : "");
  $("#cl-box").classList.remove("hidden");
}
async function clDelete() {
  const id = (W.job || {}).id || "";
  if (!id || !clInfo || !clInfo.exists) { renderModel(); go("s-model"); return; }
  if (!confirm("ダウンロード途中のファイルを削除しますか?\nこの操作は取り消せません。")) return;
  const b = $("#cl-del");
  b.disabled = true; b.textContent = "削除中…";
  try {
    const r = await api(`/api/installs/${id}/partial`, { method: "DELETE" });
    $("#cl-sub").textContent = (r.bytes ? bytes(r.bytes) : "0 B") + " を削除しました。"
      + (r.partial_only ? " (未完のファイルのみ)" : "") + "モデル選びに戻ります。";
    $("#cl-box").classList.add("hidden");
    $("#cl-del").classList.add("hidden");
    $("#cl-keep").textContent = "モデル選びに戻る";
    $("#cl-keep").disabled = false;
  } catch (e) {
    $("#cl-err").textContent = "削除できませんでした: " + e.message;
    $("#cl-err").classList.remove("hidden");
    b.disabled = false; b.textContent = "削除する";
  }
}

/* ------------------------------------------------- 3b: 稼働設定 (調整の前) */
/* キャリブレーションは コンテキスト長 / KV形式 / 並列数 を前提に計測するため、
   調整の前にこの 3 値をプロファイルへ保存する。起動 JSON 主体のバックエンド
   (regen) は保存後に再生成しないと調整が古い argv で走る。 */
/* 選択肢は「数値と単位」だけを書き、説明文は入力欄に埋め込まない
   (見出し下の 1 行 sec.hint に集約する)。行は [値, 表示, 既定タグ] の 3 つ組。
   「推奨」は固定値を焼かない — この PC の GPU/RAM と選択中の並列数などから
   cfRecommend() が描画時に計算してタグを付ける (検出できない環境では出さない)。 */
const CF_TOK = v => { const n = Math.round(Number(v));
  if (!Number.isFinite(n)) return String(v);
  if (n >= 1048576 && n % 1048576 === 0) return `${Math.round(n / 1048576)}M`;
  return (n >= 1024 && n % 1024 === 0) ? `${Math.round(n / 1024)}K` : n.toLocaleString(); };
const CF_NUM = v => { const n = parseInt(v, 10); return Number.isFinite(n) ? String(n) : String(v); };
const CF_CTX = [["32768", "32K", ""], ["131072", "128K", ""], ["262144", "256K", ""],
                ["524288", "512K", ""], ["1048576", "1M", ""]];
const CF_KV = [["fp8", "fp8", ""], ["bf16", "bf16", ""], ["auto", "auto", ""]];
/* FreeToken (fork) の --kv-cache-dtype: fp8 (静的スケール) / int8 (per-token-head、Strata 相当)
   はどちらも TP 可。auto / fp8_per_token_head (TP1 + bf16 専用)。bf16 は無い。 */
const CF_KV_FT = [["fp8", "fp8", ""], ["int8", "int8", ""], ["auto", "auto", ""],
                  ["fp8_per_token_head", "fp8/head", ""]];
const CF_PAR = [["1", "1", ""], ["2", "2", ""], ["4", "4", ""], ["8", "8", ""]];
const CF_TP = [["1", "1", ""], ["2", "2", ""], ["4", "4", ""], ["8", "8", ""]];
/* 最大出力トークン (max_tokens 省略時の生成上限)。コード生成は1応答が長くなりやすいので
   32K を推奨 (エンジン既定と同じ。ファイル丸ごとの生成にも十分で、KV予約的にも並列を落とさない)。
   これは環境に依存しない workload 推奨 — cfRecommend() が選択 ctx を超えない範囲で適用する。 */
const CF_OUT = [["8192", "8K", ""], ["16384", "16K", ""], ["32768", "32K", ""],
                ["65536", "64K", ""], ["131072", "128K", ""]];
/* 質問の固定順 (FreeToken 基準)。描画/補足/保存の 3 ループはリテラルのまま、
   推奨計算などの追加ループはこちらを使う。 */
const CF_ORDER = ["ctx","out","kv","tp","par","vis"];

/* ---------------------------------------------------------------- 環境適応の推奨 (純粋関数)
   色々な環境で使うため、「推奨」はコードに焼いた固定値ではなく、この PC の
   GPU VRAM / compute capability・CPU RAM・選択中のコンテキスト/KV/GPU分割/並列数
   から計算する目安。GPU が検出できない環境 (非 NVIDIA 等) では環境ベースの推奨を一切出さない。
   以下の 3 関数は DOM / W に依存しない純粋関数 (node で単体テストできる)。 */

/* /api/system スナップショット → 推奨の入力。GPU が無ければ null (= 環境ベースの推奨なし)。
   VRAM 降順に並べる (TP の「先頭 n 枚」が大きいカード側になるように)。 */
function hwFromSystem(sys) {
  const gpus = (((sys || {}).gpus) || [])
    .filter(g => Number(g.mem_total_mib) > 0)
    .map(g => ({ index: String(g.index), name: String(g.name || "GPU"),
                 vramGiB: Number(g.mem_total_mib) / 1024,
                 cc: Number(g.compute_cap) > 0 ? Number(g.compute_cap) : 0 }))
    .sort((a, b) => b.vramGiB - a.vramGiB);
  if (!gpus.length) return null;
  const total = ((((sys || {}).host) || {}).memory || {}).total;
  return { gpus, ramGiB: Number(total) > 0 ? Number(total) / 2 ** 30 : null };
}

/* KV 1 トークンあたりのバイト数の保守的な目安。基準は Qwen3-30B-A3B クラス MoE
   (48 layers × 4 KV heads × 128 dim × 2 (K+V) = 96 KiB/token bf16、fp8/int8 は半減)。
   dense な小型モデルには過大見積もり = 推奨は常に安全側に倒れる (実測は調整⑤の仕事)。 */
function cfKvBytes(kv) {
  const k = String(kv || "").toLowerCase();
  if (k === "fp8" || k === "int8" || k === "fp8_per_token_head") return 48 * 1024;
  if (k === "q4_0") return 24 * 1024;
  if (k === "k8v4") return 36 * 1024;
  return 96 * 1024;   // bf16 / fp16 / auto / 不明 = 非圧縮 (保守的)
}

/* backend + 環境 + 現在の選択 + Strata / Maya エンジン値 → 質問ごとの推奨 {v, why}。
   sel = {ctx, kv, tp, par, gpu} (ctx は freetoken では 1 人あたり)、
   engine = {parallel, recContext} (strata / maya の config 由来。無ければ null)。
   予算則 (2026-11-15 方針): VRAM は LLM 専用なので、選択中カードの VRAM 全量を
   KV 予算の上限とする (一律の重み予約は計算外 — 重みの配置と起動時の実空き検査は
   エンジンの仕事。Strata は config の parallel / rec_context が常に優先)。
   推奨は上限目安であり、最適値はキャリブレーション (⑤) が実測する。 */
function cfRecommend(backend, hw, sel, engine) {
  sel = sel || {};
  const rec = { ctx: null, kv: null, par: null, out: null, vis: null };
  const ctxN = Number(sel.ctx) > 0 ? Number(sel.ctx) : 131072;
  /* out: コード生成既定 32K (workload 推奨・環境非依存)。選択 ctx は超えない。 */
  const outOpts = [8192, 16384, 32768, 65536, 131072];
  const outFit = outOpts.filter(n => n <= ctxN);
  rec.out = { v: String(outFit.includes(32768) ? 32768 : (outFit[outFit.length - 1] || 8192)),
              why: "コード生成向け既定 (エンジン既定と同じ)" };
  if (backend === "basalt")
    rec.vis = { v: "dynamic", why: "エンジンが必要時に読む (常駐VRAMなし)" };
  if (!hw) return rec;   // GPU 不明 → 環境ベースの推奨は出さない (workload 推奨だけ)

  /* 使うカードの VRAM 合計: strata/basalt/maya はカード選択に従う。
     他は TP 数 n 枚分 (既定 1、大きいカード順)。同一構成想定で n × 最小 VRAM。 */
  let cards = hw.gpus;
  if (backend === "strata" || backend === "basalt" || backend === "maya") {
    const g = String(sel.gpu ?? "auto").trim();
    if (g && g !== "auto") {
      const idx = g.split(",").map(s => s.trim());
      const picked = hw.gpus.filter(x => idx.includes(x.index));
      if (picked.length) cards = picked;
    }
  } else {
    const n = Math.max(1, Math.min(Number(sel.tp) || 1, hw.gpus.length));
    const per = Math.min(...hw.gpus.slice(0, n).map(x => x.vramGiB));
    cards = hw.gpus.slice(0, n).map(x => ({ ...x, vramGiB: per }));
  }
  const vram = cards.reduce((s, x) => s + x.vramGiB, 0);
  /* fp8 対応判定は compute capability の最小値。4GiB 未満の表示用カードは
     計算に使わない前提で除外して判定する (全部小さければ全体で判定)。 */
  const big = cards.filter(x => x.vramGiB >= 4);
  const ccMin = Math.min(...(big.length ? big : cards).map(x => x.cc));
  /* VRAM は LLM 専用前提で全量を予算の上限とする (2026-11-15 方針: 一律の重み予約は
     計算外。重みの配置と起動時の実空き検査はエンジン側 — Strata はエンジン値が優先)。 */
  const budget = vram;
  const kvB = cfKvBytes(sel.kv);
  const parNow = Number(sel.par) > 0 ? Number(sel.par) : 1;
  /* RAM 上限: 1 スロットあたりホスト側バッファ 4GiB 目安、RAM の半分まで。 */
  const ramCap = hw.ramGiB ? ([1, 2, 4, 8].filter(p => p * 4 <= hw.ramGiB * 0.5).pop() || 1) : 8;
  const vramTxt = `VRAM ${Math.round(vram)}GB`;
  const budgetTxt = `KV予算 約${Math.floor(budget)}GB (${vramTxt} 全量・LLM 専用前提 — 重み配置はエンジン任せ)`;

  /* ctx / par: basalt は KV を全員で共有 (総量 = ctx、並列で割る)。
     それ以外は 1 スロットあたり ctx × 並列数 が総 KV。 */
  const shared = backend === "basalt";
  /* 選択肢は CF_CTX (32K〜1M) と揃える。basalt だけ 8K (既定) も持つ */
  const ctxOpts = [32768, 131072, 262144, 524288, 1048576];
  if (backend === "basalt") ctxOpts.unshift(8192);
  const ctxFit = ctxOpts.filter(c => (shared ? 1 : parNow) * c * kvB / 2 ** 30 <= budget);
  const ctxV = ctxFit.length ? ctxFit[ctxFit.length - 1] : ctxOpts[0];
  rec.ctx = { v: String(ctxV),
    why: ctxFit.length
      ? `${budgetTxt} ÷ ${shared ? "共有KV" : parNow + "並列"} → ${CF_TOK(ctxV)} が収まる`
      : `${budgetTxt} — 最小でも収まらない見込み。コンテキスト短縮 / GPU分割増 / KV圧縮を検討` };
  const slot = ctxN * kvB / 2 ** 30;
  const parFit = [1, 2, 4, 8].filter(p => (shared ? 1 : p) * slot <= budget);
  let parV = parFit.length ? parFit[parFit.length - 1] : 1;
  let parWhy = shared
    ? `${budgetTxt} (KVは全員で共有 — 並列数を増やしても総量は増えない)`
    : `${budgetTxt} ÷ 1スロット約${slot.toFixed(1)}GB (${CF_TOK(ctxN)} × ${sel.kv || "bf16"})`;
  if (shared) parV = vram >= 16 ? 4 : vram >= 8 ? 2 : 1;
  if (!shared && slot > budget) parWhy += " — 1スロットでも収まらない見込み";
  if (parV > ramCap) { parV = ramCap; parWhy += ` / RAM ${Math.round(hw.ramGiB)}GB 上限`; }
  rec.par = { v: String(parV), why: parWhy + " (上限目安)" };

  /* KV 形式: VRAM に余裕が無い (<48GB) なら圧縮、余裕があれば品質優先 / エンジン既定。
     fp8 は compute capability 8.0 (Ampere) 以上と確認できたときだけ推奨する。 */
  const tight = vram < 48;
  if (backend === "sglang" || backend === "vllm")
    rec.kv = tight && ccMin >= 8
      ? { v: "fp8", why: `${vramTxt} はタイト + fp8 対応 GPU → メモリ半減` }
      : { v: "bf16", why: tight ? "GPU の fp8 対応が不明 → 安全な形式" : `${vramTxt} に余裕 → 品質優先` };
  else if (backend === "freetoken-fork")
    rec.kv = tight && ccMin >= 8
      ? { v: "fp8", why: `${vramTxt} はタイト + fp8 対応 GPU → メモリ半減` }
      : tight && ccMin >= 7.5
        ? { v: "int8", why: `${vramTxt} はタイト → メモリ半減 (int8)` }
        : { v: "auto", why: `${vramTxt} に余裕 → エンジンにまかせる` };
  else if (backend === "strata" || backend === "maya" || backend === "basalt")
    rec.kv = tight ? { v: "int8", why: `${vramTxt} はタイト → メモリ半減` }
           : { v: "fp16", why: `${vramTxt} に余裕 → 品質優先` };

  /* Strata / Maya: エンジン自身が決めた値 (config の parallel / 純正公式の rec_context) が
     あれば、こちらの目安より優先する (純正の VRAM/RAM 計算が単一の源)。 */
  if ((backend === "strata" || backend === "maya") && engine) {
    const nm = backend === "maya" ? "Maya" : "Strata";
    if (Number(engine.parallel) > 0)
      rec.par = { v: String(engine.parallel),
                  why: nm + (backend === "maya" ? " config の --parallel (前回の設定)"
                             : " エンジンの VRAM 予算判定 (config の parallel)") };
    if (Number(engine.recContext) > 0) {
      const fit = ctxOpts.filter(c => c <= Number(engine.recContext));
      rec.ctx = { v: String(fit.length ? fit[fit.length - 1] : ctxOpts[0]),
                  why: `${nm} 純正の公式 (この PC の VRAM/RAM) による推奨 ${CF_TOK(engine.recContext)}` };
    }
  }
  return rec;
}
/* ---------------------------------------------------------------- 環境適応の推奨ここまで */
/* perUser 項目 (FreeToken の一人あたり設定): 画面は 1 人あたりで聞き、JSON へは
   合計 = 1人あたり × 並列数 を書く。--num-tokens は全員で共有する KV 総量なので、
   1人256K・4並列なら 1M を書かないと 1 人あたり 64K しか残らない。 */
function cfParNow() {
  const el = document.querySelector("#cf-par");
  const n = parseInt(el && el.value, 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}
/* 保存値 (curs) と並列数から推奨計算用の選択を作る。perUser の ctx は合計で
   保存されているので 1人あたりへ戻す。tp スロットは strata/basalt ではカード選択。 */
function cfSelFrom(curs, par) {
  const cfg = W.cfgRendered || RUNCFG[W.backend] || {};
  const sel = { par, ctx: "", kv: "", tp: "", gpu: "" };
  for (const id of CF_ORDER) {
    const sec = cfg[id]; if (!sec) continue;
    let val = String(curs[id] || "");
    if (sec.perUser && val) {
      const n = Math.round(Number(val) / par);
      if (Number.isFinite(n) && n > 0) val = String(n);
    }
    if (id === "ctx") sel.ctx = val;
    else if (id === "kv") sel.kv = val;
    else if (id === "tp") { sel.tp = val; sel.gpu = val; }
  }
  return sel;
}
/* フォームの現在の選択を読む (描画後の推奨再計算用) */
function cfSelNow() {
  const sel = { par: cfParNow(), ctx: "", kv: "", tp: "", gpu: "" };
  for (const id of CF_ORDER) {
    const el = document.querySelector(`#cf-${id}`);
    if (!el) continue;
    if (id === "ctx") sel.ctx = el.value;
    else if (id === "kv") sel.kv = el.value;
    else if (id === "tp") { sel.tp = el.value; sel.gpu = el.value; }
  }
  return sel;
}
/* 各選択肢の（推奨）タグを現在の環境 + 選択に合わせて付け替える。
   ユーザーの選択自体は動かさない (タグと根拠の表示だけ)。 */
function cfApplyRec() {
  const cfg = W.cfgRendered || RUNCFG[W.backend] || {};
  const recs = cfRecommend(W.backend, W.hw, cfSelNow(), W.cfgEngine);
  for (const id of CF_ORDER) {
    const el = document.querySelector(`#cf-${id}`);
    if (!el || !cfg[id]) continue;
    const r = recs[id];
    for (const opt of el.options) {
      if (opt.dataset.raw === "1") continue;   // 「現在の値」の逃避行は触らない
      const isRec = !!(r && opt.value === r.v);
      opt.textContent = (opt.dataset.base || opt.textContent)
        + (opt.dataset.tag ? `（${opt.dataset.tag}）` : "")
        + (isRec ? "（推奨）" : "");
    }
  }
  return recs;
}
/* 検出したハードウェアの要約 (cf-sub の根拠表示用): 同名カードは枚数でまとめる */
function hwSummary(hw) {
  const by = {};
  hw.gpus.forEach(g => { (by[g.name] = by[g.name] || { n: 0, vram: g.vramGiB }).n++; });
  const gpuTxt = Object.entries(by)
    .map(([n, x]) => `${n}${x.n > 1 ? " ×" + x.n : ""} (VRAM ${Math.round(x.vram)}GB)`).join(" / ");
  return gpuTxt + (hw.ramGiB ? ` · RAM ${Math.round(hw.ramGiB)}GB` : "");
}
/* 見出し下の補足 1 行。perUser は「1人あたり × 並列 = 合計KV」を生きた値で示し、
   推奨がある項目は根拠 (VRAM/RAM/並列数からの目安) を追記する */
function cfSyncHints() {
  const cfg = W.cfgRendered || RUNCFG[W.backend] || {};
  const recs = cfApplyRec();
  for (const id of ["ctx", "out", "kv", "tp", "par", "vis"]) {
    const sec = cfg[id]; const hint = document.querySelector(`#cf-${id}-hint`);
    if (!sec || !hint) continue;
    hint.textContent = sec.hint || "";
    if (sec.perUser) {
      const el = document.querySelector(`#cf-${id}`);
      const n = parseInt(el ? el.value : "", 10);
      if (Number.isFinite(n) && n > 0) {
        const par = cfParNow();
        const per = el.selectedOptions[0] && el.selectedOptions[0].dataset.raw === "1"
          ? Math.round(n / par) : n;
        hint.innerHTML = esc(sec.hint)
          + `<br>1人あたり ${esc(CF_TOK(per))} × ${par}並列 = KV合計 ${esc(CF_TOK(per * par))}`;
      }
    }
    const r = recs[id];
    if (r && r.why) hint.innerHTML += `<br>推奨の根拠: ${esc(r.why)}`;
  }
}
/* 稼働設定 (③) の質問セット。画面の質問順は全バックエンド共通で
   ctx → kv → tp → par → vis の固定順 (FreeToken のセットアップが基準)。
   バックエンド追加時は実在するフラグの項目だけを入れる (順序は定義側に依存しない)。 */
const RUNCFG = {
  "freetoken-fork": { regen: true, model: true,
    ctx: { key: "num_tokens", label: "一人当たりのコンテキスト長", short: "1人あたり", perUser: true,
           hint: "長いほどVRAMを消費します。調整はこの長さ前提のKVキャッシュで行います", opts: CF_CTX, cur: CF_TOK },
    out: { key: "max_output_tokens", label: "最大出力トークン", short: "出力",
           hint: "1回の応答で生成する上限。コード生成向けに32Kを推奨。大きくすると1リクエストあたりのKV予約が増えます",
           opts: CF_OUT, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式",
          hint: "会話履歴の格納精度です。fp8とint8はメモリ半減でTPも可。fp8/headはTP1+bf16専用です",
          opts: CF_KV_FT, cur: v => v },
    tp: { key: "tp_size", label: "Tensor Parallel (GPU分割数)", short: "GPU分割",
          hint: "モデルを何枚のGPUに分割するか。VRAMが足りない場合は増やします", opts: CF_TP, cur: CF_NUM },
    par: { key: "max_running_requests", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: CF_NUM } },
  /* freetoken (upstream) も fork と同じ導線 (稼働設定 → 接続設定 → 調整) に揃える。
     欠落していると ③④ が丸ごとスキップされ、プロファイルも作られない。
     ctx/tp/par は upstream にも実在 (--num-tokens / --tp-size / --max-running-requests)。
     kv は upstream に --kv-cache-dtype が無いため聞かない。 */
  "freetoken": { regen: true, model: true,
    ctx: { key: "num_tokens", label: "一人当たりのコンテキスト長", short: "1人あたり", perUser: true,
           hint: "長いほどVRAMを消費します。調整はこの長さ前提のKVキャッシュで行います", opts: CF_CTX, cur: CF_TOK },
    out: { key: "max_output_tokens", label: "最大出力トークン", short: "出力",
           hint: "1回の応答で生成する上限。コード生成向けに32Kを推奨。大きくすると1リクエストあたりのKV予約が増えます",
           opts: CF_OUT, cur: CF_TOK },
    tp: { key: "tp_size", label: "Tensor Parallel (GPU分割数)", short: "GPU分割",
          hint: "モデルを何枚のGPUに分割するか。VRAMが足りない場合は増やします", opts: CF_TP, cur: CF_NUM },
    par: { key: "max_running_requests", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: CF_NUM } },
  "sglang": { regen: false,
    ctx: { key: "context_length", label: "コンテキスト長",
           hint: "長いほどVRAMを消費します", opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式",
          hint: "fp8はメモリ半減、bf16は品質優先", opts: CF_KV, cur: v => v },
    tp: { key: "tp_size", label: "Tensor Parallel (GPU分割数)", short: "GPU分割",
          hint: "モデルを何枚のGPUに分割するか。VRAMが足りない場合は増やします", opts: CF_TP, cur: CF_NUM },
    par: { key: "max_running_requests", label: "並列推論数",
           hint: "同時に処理するリクエスト数", opts: CF_PAR, cur: CF_NUM } },
  "vllm": { regen: false,
    ctx: { key: "max_model_len", label: "コンテキスト長",
           hint: "長いほどVRAMを消費します", opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式",
          hint: "fp8はメモリ半減、bf16は品質優先", opts: CF_KV, cur: v => v },
    tp: { key: "tensor_parallel_size", label: "Tensor Parallel (GPU分割数)", short: "GPU分割",
          hint: "モデルを何枚のGPUに分割するか。VRAMが足りない場合は増やします", opts: CF_TP, cur: CF_NUM },
    par: { key: "max_num_seqs", label: "並列推論数",
           hint: "同時に処理するリクエスト数", opts: CF_PAR, cur: CF_NUM } },
  /* strata: values.config は ② のモデル構成 (純正 setup.py) が作った strata-<model>.json。
     コンテキスト長 / KV 形式は config の args に入るが、FreeToken 基準と同じ質問を
     ③ で出し、保存時に config-args で書き戻す (並列数だけは serve CLI のフラグ)。 */
  "strata": { regen: "config-args", model: "config",
    ctx: { key: "max_context", flag: "--max-context", config: true, label: "コンテキスト長",
           hint: "長いほどVRAMを消費します。調整はこの長さ前提のKVキャッシュで行います",
           opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv", flag: "--kv", config: true, label: "KVキャッシュ形式",
          hint: "会話履歴の格納精度です。int8はメモリ半減、fp16は品質優先",
          opts: [["int8", "int8", ""], ["fp16", "fp16", ""],
                 ["q4_0", "q4_0", ""], ["k8v4", "k8v4", ""]], cur: v => v },
    /* 純正 setup.sh が聞く「複数枚使うか」を FreeToken 基準の GPU 分割 (tp) 位置で聞く。
       選択肢は検出カードから描画時に組み立てる ( usable 2 枚未満なら質問自体を出さない )。
       書き戻し先は config の gpu / layer_split (args ではない) */
    /* key は gpu_split (--gpu フィールドの key "gpu" と衝突させない。CLI の --gpu は
       単一カード専用で "0,1" を送ると起動が壊れる。config が正なので値は記録のみ) */
    tp: { key: "gpu_split", flag: "gpu", config: true, label: "GPU の使い方 (複数GPU共有)", short: "GPU",
          hint: "2枚以上なら layer split でモデルを分け、約2倍のエキスパートを保持できます",
          opts: [], cur: v => v },
    par: { key: "parallel", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: CF_NUM } },
  /* maya: strata 系。コンテキスト / KV / TP は maya-<model>.json の args、並列数は maya.py CLI。
     質問は家系別に出し分ける (renderConfig が config-args の family / kv_ok / tp_ok を見て
     kv を削除 / tp を GPU の使い方 (config の gpu = layer split) に差し替える):
     GLM / Qwen / MiniMax GGUF = ctx + kv + GPU / DeepSeek / MiniMax EXL3 = ctx + tp */
  "maya": { regen: "config-args", model: "config",
    ctx: { key: "max_context", flag: "--max-context", config: true, label: "コンテキスト長",
           hint: "長いほどVRAMを消費します。調整はこの長さ前提のKVキャッシュで行います",
           opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv", flag: "--kv", config: true, label: "KVキャッシュ形式",
          hint: "会話履歴の格納精度です。int8はメモリ半減、fp16は品質優先",
          opts: [["int8", "int8", ""], ["fp16", "fp16", ""]], cur: v => v },
    tp: { key: "tp", flag: "--tp", config: true, label: "Tensor Parallel (GPU分割数)", short: "GPU分割",
          hint: "モデルを何枚のGPUに分割するか。VRAMが足りない場合は増やします", opts: CF_TP, cur: CF_NUM },
    par: { key: "parallel", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: CF_NUM } },
  /* basalt: コンテキスト / KV / 並列は設定 JSON が正。保存で自動再生成する。
     調整 (キャリブレーション) は無いので s-cal は自動でスキップされる。
     複数 GPU は gpu のカード選択 (engine が expert tier / layer split を自動選択。
     tensor parallel は価格評価のみで engine 未実装 = checkout / upstream とも同じ)。
     FreeToken 基準の GPU 分割質問と同じ位置で gpu を聞く */
  "basalt": { regen: "config-json", model: "model",
    ctx: { key: "max_context", label: "コンテキスト長",
           hint: "プロンプト+生成で共有します。並列数を増やすと 1 スロットあたりは コンテキスト÷並列 になります",
           opts: [["8192", "8K", "既定"], ["32768", "32K", ""],
                  ["131072", "128K", ""], ["262144", "256K", ""],
                  ["524288", "512K", ""], ["1048576", "1M", ""]], cur: CF_TOK },
    kv: { key: "kv", label: "KVキャッシュ形式",
          hint: "fp16=品質優先 / int8=VRAM半減 / q4_0・k8v4=さらに圧縮",
          opts: [["fp16", "fp16", "既定"], ["int8", "int8", ""],
                 ["q4_0", "q4_0", ""], ["k8v4", "k8v4", ""]], cur: v => v },
    tp: { key: "gpu", label: "GPU の使い方", short: "GPU", gpusFrom: "system",
          hint: "auto=全 Blackwell カード / 番号=そのカード (先頭がメイン)。共有方式はエンジンが自動選択",
          opts: [], cur: v => v },
    par: { key: "parallel", label: "並列推論数",
           hint: "同時に処理するリクエスト数 (1-8)。コンテキスト長はこの数で分割されます",
           opts: CF_PAR, cur: CF_NUM },
    vis: { key: "vision", label: "画像入力 (ビジョン)",
           hint: "dynamic=エンジン内で読む (常駐VRAMなし)。off にすると画像リクエストはエラーになります",
           opts: [["dynamic", "dynamic", ""], ["off", "off", ""], ["cpu", "cpu", ""]], cur: v => v } },
};
/* 設定先行フローのプロファイル名既定: ② の選択から (config 名 / repo 名)。
   ジョブの成果物がまだ無いので、予測される config 名を使う */
function preJobName() {
  if (W.tableReq) {
    const f = (mdT().families || {})[W.tableReq.family] || {};
    return mdCfgKey(f, W.tableReq.model)
      .replace(/^strata-/, "").replace(/^maya-/, "").replace(/\.json$/, "");
  }
  if (W.dlReq) return String(W.dlReq.repo_id).split("/").pop();
  return specName(W.backend);
}
async function renderConfig() {
  W.cfgSkip = true;
  // 浅いコピーで受ける: Maya の家系別出し分け (kv / tp の削除・差し替え) が
  // RUNCFG の共有定義を壊さないように (再描画・他バックエンドで再利用する)
  const cfg = RUNCFG[W.backend] ? { ...RUNCFG[W.backend] } : null;
  /* 推奨用のハードウェアスナップショット (/api/system、ページにつき 1 回)。
     basalt の GPU 選択肢も同じ源を使う。取得失敗 / GPU 無し → 環境ベースの推奨は出さない。 */
  if (W.sysInfo === undefined) {
    try { W.sysInfo = await api("/api/system"); } catch { W.sysInfo = null; }
  }
  W.sysGpus = ((W.sysInfo || {}).gpus) || [];
  W.hw = hwFromSystem(W.sysInfo);
  let p = null;
  try {
    const j = W.job;
    const profs = await api("/api/profiles");
    if (W.cfgProfId) {
      // 戻る/次への往復では、このセットアップで作ったプロファイルを再利用
      // (バックエンドが変わっていたら掴み直さない — ① まで戻って選び直した場合)
      p = (profs || []).find(x => x.id === W.cfgProfId && x.backend === W.backend) || null;
    }
    if (!p && cfg) {
      // セットアップのたびに専用プロファイルを新規作成する。
      // 既存 (前回のセットアップで作ったものを含む) を再利用すると、その
      // プロファイルの設定 (コンテキスト長 / 並列など) と JSON を上書きして
      // しまい、設定違いのプロファイルを増やせなくなる。
      const nm = (j && j.config_name) ? String(j.config_name).replace(/^strata-/, "").replace(/^maya-/, "").replace(/\.json$/, "")
                 : (j && j.repo_id) ? String(j.repo_id).split("/").pop()
                 : W.preJob ? preJobName()
                 : specName(W.backend);
      // config 主体 (strata/maya) は ② の成果物 (config JSON) を作成時から結びつける。
      // ③ のプリフィル (config-args) と保存時の書き戻しがこのパスを基準にする。
      // 設定先行フロー (preJob) では config はまだ無い — ジョブ完了時に紐付ける
      const initVals = {};
      const jcfg = String(((j || {}).result || {}).config || "").trim();
      if (cfg.model === "config" && jcfg) initVals.config = jcfg;
      p = await api("/api/profiles", { method: "POST",
        body: { name: nm, backend: W.backend, values: initVals, simple_visible: true,
                note: profileFlow() ? "プロファイルウィザードで作成" : "初回セットアップで作成" } });
      W.cfgProfNew = true;
    }
    if (p) W.cfgProfId = p.id;
  } catch { /* 取得失敗は調整画面へスキップ */ }
  if (!cfg || !p) { renderCal(); go("s-cal"); return; }
  W.cfgSkip = false;
  W.calProfile = p;
  const v = p.values || {};
  // config に入る質問 (strata/maya の ctx/kv/tp) は config から現在値をプリフィル。
  // 設定先行フローでは config がまだ無いので、② の表 (家系・検出 GPU) から組み立てる
  W.cfgArgs = {};
  if (W.preJob && mdTableBackend() && W.tableReq) {
    const st = mdT();
    const fam = (st.families || {})[W.tableReq.family] || {};
    const isMaya = W.backend === "maya";
    const usable = ((st.pc || {}).gpus || []).filter(g => g.usable).map(g => g.index);
    W.cfgArgs = { gpus: usable, gpu_multi: fam.multi_gpu !== false,
                  family: isMaya ? W.tableReq.family : undefined,
                  kv_ok: isMaya ? (W.tableReq.family === "glm" || W.tableReq.family === "qwen") : true,
                  tp_ok: isMaya && (W.tableReq.family === "deepseek"
                                    || (W.tableReq.family === "minimax" && W.tableReq.model === "exl3")) };
  } else if (Object.values(cfg).some(s => s && s.config)) {
    try {
      const r = (await api(`/api/profiles/${p.id}/config-args`)) || {};
      W.cfgArgs = { ...(r.args || {}), gpu: r.gpu, gpus: r.gpus, gpu_multi: r.gpu_multi,
                    family: r.family, kv_ok: r.kv_ok, tp_ok: r.tp_ok,
                    source_config: r.source_config || "" };
    } catch { /* 読めなければ未設定扱い (推奨プリチェック) */ }
  }
  /* Maya: 家系別の質問出し分け (エンジン実装が単一の源)。
     GLM / Qwen / MiniMax GGUF = --kv + GPU の使い方 (config の gpu = layer split。--tp は無い)。
     DeepSeek / MiniMax EXL3 = GPU の使い方 + --tp (導出)。--kv は無い (KV はエンジン固有)。
     Qwen は --parallel 非対応 (1 リクエストずつ) — 並列数の質問自体を出さない
     (2 以上を保存すると起動が check_parallel で失敗するため)。 */
  if (W.backend === "maya" && W.cfgArgs.family) {
    if (W.cfgArgs.kv_ok === false) delete cfg.kv;
    if (W.cfgArgs.family === "qwen") delete cfg.par;
    /* GPU の使い方は全家系で聞く (純正 choose_gpus と同じ立場)。DS4 / MM3-EXL3 は
       カード選択から --tp を純正同款 min(2, 枚数) で導出する — --tp だけ聞いて
       config の gpu を放置すると「tp=1 なのに 2 枚 layer split」で起動 / 計測される
       (2026-11-16 実害: gpu:[0,1] + --tp 1)。保存は cf-go の tpFromGpus が担う */
    cfg.tp = { key: "gpu_split", flag: "gpu", config: true, label: "GPU の使い方 (複数GPU共有)", short: "GPU",
               hint: W.cfgArgs.tp_ok
                 ? "使うカードを選びます。Tensor Parallel (--tp) は枚数から自動で決まり (純正同じ: min(2, 枚数))、調整 (⑤) が実測で詰めます"
                 : "2枚以上なら layer split でモデルを分け、より多くのエキスパートを VRAM/RAM に保持できます",
               opts: [], cur: v => v, tpFromGpus: W.cfgArgs.tp_ok === true };
  }
  /* 設定先行フロー: コンテキスト長は ② で既に選んでいる (セットアップが config に書く)。
     ③ で二重に聞かない */
  if (W.preJob && mdTableBackend()) delete cfg.ctx;
  // GPU の使い方 (strata / maya の layer split 系) は純正と同じく usable 2 枚以上のときだけ質問する。
  // 選択肢は検出カードから組み立てる (単一カード / 全カード layer split / まかせる)
  if (cfg.tp && cfg.tp.flag === "gpu") {
    const cards = W.cfgArgs.gpus || [];
    const opts = [["auto", `まかせる (${W.backend === "maya" ? "Maya" : "Strata"} 推奨)`, ""]];
    cards.forEach(i => opts.push([String(i), `GPU ${i} のみ`, ""]));
    if (cards.length >= 2 && W.cfgArgs.gpu_multi !== false)
      opts.push([cards.join(","), `全カード共有 (${cards.join(", ")}) — layer split`, ""]);
    cfg.tp = { ...cfg.tp, opts };
  }
  // basalt の GPU の使い方: 選択肢は /api/system の検出結果から (2 枚以上のときだけ質問)
  if (cfg.tp && cfg.tp.gpusFrom === "system") {
    const idx = W.sysGpus.map(g => String(g.index));
    const opts = [["auto", "auto (全 Blackwell)", ""]];
    idx.forEach(i => opts.push([i, `GPU ${i} のみ`, ""]));
    if (idx.length >= 2) opts.push([idx.join(","), `${idx.join(",")} (先頭がメイン)`, ""]);
    cfg.tp = { ...cfg.tp, opts };
  }
  /* Strata / Maya のエンジン値: config の parallel (Strata: VRAM 予算判定 / Maya: args の
     --parallel) と純正公式の rec_context。config 名から家系/モデルを復元するので、
     ページリロード後でも同じ推奨が出る。 */
  W.cfgEngine = null;
  if (W.backend === "strata" || W.backend === "maya") {
    const isMaya = W.backend === "maya";
    const st = await (isMaya ? loadMaya() : loadStrata());
    if (W.preJob && W.tableReq) {
      // config はまだ無い: ② で選んだモデルの純正推奨コンテキストだけ使う
      // (並列数はユーザーがここで決める — セットアップジョブに渡す)
      const f = (st.families || {})[W.tableReq.family] || {};
      const d = (f.models || {})[W.tableReq.model] || {};
      W.cfgEngine = { parallel: null, recContext: Number(d.rec_context) || null };
    } else {
      // 専用コピー (data/launch/<backend>-<slug>-<uuid8>.json) は source_config で
      // 元の installer config 名を引く (純正表の parallel / rec_context 突合用)
      const cfgName = String(W.cfgArgs.source_config || String(v.config || "").split("/").pop());
      const c = (st.configs || []).find(x => x.name === cfgName);
      let recCtx = null;
      for (const f of Object.values(st.families || {})) {
        for (const [m, d] of Object.entries(f.models || {})) {
          const key = isMaya ? String(d.config || "")
                             : "strata-" + String((f.tag || "") + m).toLowerCase() + ".json";
          if (cfgName && cfgName === key) recCtx = Number(d.rec_context) || null;
        }
      }
      const jp = (W.job || {}).result || {};
      W.cfgEngine = { parallel: Number((c || {}).parallel) || Number(jp.parallel) || null,
                      recContext: recCtx };
    }
  }
  /* 実際に描画する質問セットを記録する: 保存 (cf-go) / 推奨タグ / 補足は
     この出し分け込みの定義を使う (RUNCFG の静的定義だと Maya GLM で
     「GPU の使い方」の選択を --tp として書いてしまう) */
  W.cfgRendered = cfg;
  /* 検出環境の要約を根拠として見せる。GPU が読めない環境では「推奨なし」を正直に出す */
  const hwLine = W.hw
    ? `この PC: ${hwSummary(W.hw)} — 推奨はこのスペックと選択中の並列数からの上限目安です (VRAM は LLM 専用前提)。調整 (⑤) が実測します。`
    : "GPU を検出できませんでした — 環境ベースの推奨は表示しません。環境に合わせて選んでください (調整で実測できます)。";
  $("#cf-sub").innerHTML = (W.cfgProfNew ? "新しい起動プロファイル <b>" : "プロファイル <b>")
    + esc(p.name) + "</b> に保存します。"
    + (W.preJob ? "この後、接続設定 → ダウンロード / セットアップの順に進みます。" : "")
    + "キャリブレーションはこの設定で計測します。後からダッシュボードでも変更できます。"
    + `<br>${esc(hwLine)}`;
  const nmEl = $("#cf-name");
  if (nmEl && document.activeElement !== nmEl) nmEl.value = p.name || "";
  const parts = [];
  /* 現在値 (保存値優先) を先に集める — 推奨計算とプリフィルが同じ源を使う */
  const curs = {};
  for (const id of CF_ORDER) {
    const sec = cfg[id]; if (!sec) continue;
    curs[id] = String(v[sec.key] ?? "")
      || (sec.config ? String(W.cfgArgs[sec.flag] || "") : "");
  }
  // perUser 項目は保存値 (合計) を並列数で割って 1 人あたりとして見せる。
  // 並列数の初期値は保存値、無ければ環境ベースの推奨 (フォーム描画前なので DOM を使わない)
  const parInit = (() => {
    if (!cfg.par) return 1;
    const s = parseInt(curs.par, 10);
    if (Number.isFinite(s) && s > 0) return s;
    /* strata / maya: 並列数の既定は作られた config の parallel (Strata: エンジンが VRAM
       予算で決めた値 / Maya: args の --parallel)。config に無い = 純正既定の 1。
       全局推奨の 4 を焼くと 24GB で 4 スロット起動失敗 (qwen36 131072 = 5.56 GiB/スロット、
       2026-11-09 実測) */
    if (W.backend === "strata" || W.backend === "maya") {
      const jp = (W.job || {}).result || {};
      return Number((W.cfgEngine || {}).parallel) || Number(jp.parallel) || 1;
    }
    const r0 = cfRecommend(W.backend, W.hw, cfSelFrom(curs, 1), W.cfgEngine);
    return parseInt((r0.par || {}).v, 10) || parseInt((cfg.par.opts[0] || ["1"])[0], 10) || 1;
  })();
  const recs = cfRecommend(W.backend, W.hw, cfSelFrom(curs, parInit), W.cfgEngine);
  for (const id of ["ctx", "out", "kv", "tp", "par", "vis"]) {
    const sec = cfg[id]; if (!sec) continue;
    if (sec.flag === "gpu" && (W.cfgArgs.gpus || []).length < 2) continue;  // 純正同じく2枚以上でだけ聞く
    if (sec.gpusFrom === "system" && (W.sysGpus || []).length < 2) continue;
    const stored = curs[id];
    const perNum = sec.perUser ? Math.round(Number(stored) / parInit) : NaN;
    let cur = (sec.perUser && stored && Number.isFinite(perNum)) ? String(perNum) : stored;
    // 並列数は未保存なら parInit (strata はエンジン値) を現在の値として見せる
    if (id === "par" && cur === "") cur = String(parInit);
    // 未設定の項目は環境ベースの推奨をプリチェック (推奨が無ければ先頭。
    // GPU 不明の KV はエンジンにまかせる auto)。何も選まずに保存して値が
    // 空になる状態を防ぐ
    const recV = (recs[id] || {}).v || "";
    const def = cur === "" ? ((sec.opts.some(o => o[0] === recV) ? recV : "")
                              || (id === "kv" && !W.hw && sec.opts.some(o => o[0] === "auto") ? "auto" : "")
                              || (sec.opts[0] || [""])[0]) : null;
    // 選択肢が 2つ以上の設定はプルダウンにまとめる (画面を縦に伸ばさない)。
    // 表示は数値と単位だけにし、data-t1 にサマリーへ使う短い名称を持たせる。
    // data-base / data-tag は cfApplyRec が（推奨）タグを付け替えるための土台
    let rows = sec.opts.map(o => {
      const sel = cur ? cur === o[0] : o[0] === def;
      return `<option value="${esc(o[0])}"${sel ? " selected" : ""}`
        + ` data-t1="${esc(o[1])}" data-base="${esc(o[1])}" data-tag="${esc(o[2] || "")}">${esc(o[1])}`
        + (o[2] ? `（${esc(o[2])}）` : "") + "</option>";
    }).join("");
    if (cur && !sec.opts.some(o => o[0] === cur)) {
      // 選択肢に無い既存値 (手動設定や、合計でしか書かれない値) はそのまま維持。
      // data-raw は「JSON の値を据え置く」意味なので保存時に並列数を掛けない。
      // 並列数は未保存でも parInit (エンジン値など) を現在の値として据え置く
      const kept = stored || cur;
      const t = esc(sec.cur ? sec.cur(sec.perUser ? cur : kept)
                            : (sec.perUser ? CF_TOK(cur) : kept));
      rows = `<option value="${esc(kept)}" selected data-t1="${t}"`
        + (sec.perUser ? ' data-raw="1"' : "") + `>${t}（現在の値）</option>` + rows;
    }
    // 見出しはフローティングラベルが兼ねるので、 group 内は「選択 → 補足1行」だけ
    parts.push(`<div class="cf-group">`
      + `<div class="field sel-fixed"><select id="cf-${id}" data-name="${esc(id)}">${rows}</select>`
      + `<label for="cf-${id}">${esc(sec.label)}</label></div>`
      + `<div class="cf-hint" id="cf-${id}-hint">${esc(sec.hint)}</div></div>`);
  }
  $("#cf-groups").innerHTML = parts.join("");
  cfSyncHints();
  $("#cf-err").classList.add("hidden");
}
$("#cf-groups").addEventListener("change", e => {
  const inp = e.target.closest("input");
  if (inp && inp.type === "radio") {
    document.querySelectorAll(`#cf-groups input[name="${inp.name}"]`).forEach(i =>
      i.closest(".opt").classList.toggle("sel", i.checked));
  }
  cfSyncHints();   // 選択変更 → 1人あたり/KV合計の補足と（推奨）タグ・根拠も追従させる
});
// PUT はドキュメント全体を置き換えるため、保存のたびにサーバーの最新を再取得して
// 土台にする。古い W.calProfile のまま PUT すると、前の保存で書き込まれた
// model_path / config (起動JSON) が黙って消える (2026-10-30 の起動失敗の実害)。
async function refreshProf(p) {
  try {
    const profs = await api("/api/profiles");
    const cur = (profs || []).find(x => x.id === p.id);
    if (cur) { W.calProfile = cur; return cur; }
  } catch { /* 取れなければ手元のオブジェクトで保存を続ける */ }
  return p;
}
$("#cf-go").addEventListener("click", async () => {
  const p0 = W.calProfile;
  if (!p0) { renderCal(); go("s-cal"); return; }
  const p = await refreshProf(p0);
  const cfg = W.cfgRendered || RUNCFG[W.backend] || {};
  const err = $("#cf-err");
  err.classList.add("hidden");
  const vals = { ...(p.values || {}) };
  const on = p.on ? { ...p.on } : null;
  const chosen = [];
  const updates = {};   // config args への書き戻し (strata/maya の ctx/kv/tp)
  const par = cfParNow();   // 一人あたり項目が合計を書き込むときに使う並列数 (フォームの選択)
  for (const id of ["ctx", "out", "kv", "tp", "par", "vis"]) {
    const sec = cfg[id]; if (!sec) continue;
    const el = document.querySelector(`#cf-groups select#cf-${id}`);
    if (!el || !el.value) continue;
    const opt = el.selectedOptions[0];
    const raw = !!(opt && opt.dataset.raw === "1");
    // perUser (一人当たり設定) は JSON へ 合計 = 1人あたり × 並列数 を書く
    // (--num-tokens は全員で共有する KV 総量で、そのままだと 1 人あたりは並列数分で割れる)。
    // data-raw = 「現在の値をそのまま維持」を選ぶので掛けない
    const per = sec.perUser ? parseInt(el.value, 10) : NaN;
    vals[sec.key] = (sec.perUser && !raw && Number.isFinite(per)) ? String(per * par) : el.value;
    if (on) on[sec.key] = true;
    if (sec.config && sec.flag) updates[sec.flag] = vals[sec.key];
    /* Maya DS4 / MM3-EXL3: --tp は GPU 選択から導出 (純正 maya.py と同じ min(2, 枚数))。
       auto = 現状のカード構成 (config の gpu、無ければ全 usable) を維持する */
    if (sec.tpFromGpus) {
      const g = String(vals[sec.key] || "auto").trim();
      const cur = String(W.cfgArgs.gpu || "auto").trim();
      const base = g !== "auto" ? g : (cur !== "auto" ? cur : (W.cfgArgs.gpus || []).join(","));
      const n = Math.max(1, base.split(",").filter(x => x.trim() !== "").length);
      vals.tp = String(Math.min(2, n));
      if (on) on.tp = true;
      updates["--tp"] = vals.tp;
    }
    chosen.push((sec.short || sec.label.split(" ")[0]) + " "
      + ((opt && opt.dataset.t1) || el.value)
      + (sec.perUser && !raw ? `×${par}並列` : ""));
  }
  // モデル未決定のプロファイルは今回ダウンロードしたモデルを指す。ページ
  // リロードで W.job を失ってもインストール履歴 (jobs) から復元する。新規
  // プロファイルでモデルなしを保存すると --model-path 無しで起動不能になる
  // ため、解決できなければ保存せず前に戻す (2026-10-30 の実害の再発防止)。
  const modelKey = cfg.model === true ? "model_path" : (cfg.model || "model_path");
  /* Strata は kind=install / phase=model の「モデル構成」ジョブが result.config で
     strata-<model>.json へのパスを返す (他バックエンドは kind=download の result.path) */
  const okJob = x => x.status === "done" && x.backend === W.backend
    && (x.kind === "download" || (x.kind === "install" && x.phase === "model"));
  const pickMp = x => String((x.result && (x.result.config || x.result.path)) || x.local_dir || "");
  /* 設定先行フロー: モデル / config はジョブ完了時に紐付ける (bindSetupResult /
     bindDownloadResult)。③ の回答は pendingUpdates に溜めておき、ここでガードしない。
     「あとでする」(modelSkipped) はモデルなし完了を許す (後でプロファイル編集) */
  if (W.preJob) {
    W.pendingUpdates = updates;
    W.setupGpus = String(vals.gpu_split || "");        // layer split 回答 → セットアップの --gpus
    W.setupParallel = parseInt(vals.parallel, 10) || 0; // Maya → セットアップの --parallel
  }
  if (!W.preJob && cfg.model && !String((p.values || {})[modelKey] || "").trim()) {
    let mp = "";
    const j = W.job;
    if (j && okJob(j)) mp = pickMp(j);
    if (!mp) {
      try {
        const ins = await api("/api/installs");
        const done = ((ins && ins.jobs) || []).filter(okJob);
        const dj = done[done.length - 1];
        if (dj) mp = pickMp(dj);
      } catch { /* 履歴が取れなければ空のまま */ }
    }
    mp = await resolveBasaltPack(mp, (W.job || {}).repo_id);
    if (mp) {
      vals[modelKey] = mp;
      if (on) on[modelKey] = true;
      chosen.push("モデル " + mp.split("/").pop());
    } else if (W.cfgProfNew) {
      err.textContent = "モデルがまだ決まっていません。前に戻ってモデルをダウンロードしてください";
      err.classList.remove("hidden");
      return;
    }
  }
  /* Strata: serve/server.py の --engine 既定は mock (スクリプト付きテストエンジン)。
     ウィザードが作るプロファイルは常に実エンジンを意味するので、未設定ならここで
     埋める — 埋めないと「起動するとモックが来る」(2026-11-09 報告) */
  if (W.backend === "strata" && !String(vals.engine || "").trim()) {
    vals.engine = "strata";
    if (on) on.engine = true;
  }
  // プロファイル名 (既定はモデル名で既に入力済み。空なら元の name を維持)
  const nmEl = $("#cf-name");
  const newName = (nmEl && nmEl.value.trim()) || p.name;
  const body = { name: newName, backend: p.backend, values: vals, on,
    extra_args: p.extra_args || "", env: p.env || {}, cwd: p.cwd || "", command: p.command || "",
    custom_only: !!p.custom_only, note: p.note || "", simple_visible: !!p.simple_visible };
  const btn = $("#cf-go");
  btn.disabled = true;
  try {
    await api(`/api/profiles/${p.id}`, { method: "PUT", body });
    if (W.preJob) {
      // 設定先行フロー: config / 起動 JSON の再生成・書き戻しはモデルができた
      // ジョブ完了後 (bindSetupResult / bindDownloadResult) に行う
    } else if (cfg.regen === "config-args") {
      // strata/maya: エンジン本体のノブは config の args が正。保存後に書き戻す
      // (起動もキャリブレーションもこの config を読む)
      if (Object.keys(updates).length)
        await api(`/api/profiles/${p.id}/config-args`, { method: "POST", body: { updates } });
    } else if (cfg.regen) {
      // 調整と起動は JSON を正として読むため、保存後は必ず再生成する
      // (basalt は basalt_config.json、それ以外は従来の起動 JSON)
      await api(`/api/profiles/${p.id}/${cfg.regen === "config-json" ? "config-json" : "launch-json"}`,
                { method: "POST" });
    }
    p.name = newName;
    await refreshProf(p); // config (起動JSON) 込みの最新を次のステップへ渡す
    W.summary.push({ ok: true, t: "稼働設定を保存しました (" + chosen.join(" · ") + ")" });
    renderNet(); go("s-net");
  } catch (e) {
    err.textContent = "保存に失敗しました: " + e.message;
    err.classList.remove("hidden");
  } finally { btn.disabled = false; }
});

/* ---------------------------------------------------------------- 3: 接続設定 */
async function renderNet() {
  const p = W.calProfile;
  if (!p) { renderCal(); go("s-cal"); return; }
  const v = p.values || {};
  const hs = $("#nt-host"), ps = $("#nt-port"), ks = $("#nt-key");
  let dPort = "";
  if (!String(v.port || "").trim()) {
    try {
      if (!W.backends) {
        const st = await api("/api/state");
        W.backends = (st && st.backends) || [];
      }
      const b = (W.backends || []).find(x => x.id === W.backend);
      dPort = (b && b.default_port) ? String(b.default_port) : "";
    } catch { /* 既定ポートが取れなければ空欄 */ }
  }
  if (document.activeElement !== hs) hs.value = String(v.host || "0.0.0.0");
  if (document.activeElement !== ps) ps.value = String(v.port || dPort || "");
  if (document.activeElement !== ks) ks.value = String(v.api_key || "");
  /* 設定先行フローでは ④ が最後の質問 — 保存したら DL / セットアップを開始する
     («あとでする» でモデルなしのときは開始せず調整へ) */
  const willJob = W.preJob && !W.modelSkipped;
  $("#nt-go").textContent = willJob
    ? (W.tableReq ? "保存してセットアップを開始" : "保存してダウンロードを開始")
    : "保存して次へ";
  $("#nt-sub").textContent = "プロファイル「" + p.name + "」に保存します。"
    + (willJob ? " この後、モデルのダウンロード / セットアップを開始します (完了まで時間がかかります)。" : "");
  $("#nt-err").classList.add("hidden");
}
$("#nt-go").addEventListener("click", async () => {
  const p0 = W.calProfile;
  if (!p0) { renderCal(); go("s-cal"); return; }
  const p = await refreshProf(p0); // 稼働設定で入った値+config を消さないため最新を土台に
  const err = $("#nt-err");
  err.classList.add("hidden");
  const host = $("#nt-host").value.trim();
  const port = $("#nt-port").value.trim();
  const key = $("#nt-key").value.trim();
  if (port && (!/^\d+$/.test(port) || +port < 1 || +port > 65535)) {
    err.textContent = "ポート番号は 1〜65535 の数値で入力してください";
    err.classList.remove("hidden"); return;
  }
  const vals = { ...(p.values || {}) };
  const on = p.on ? { ...p.on } : null;
  const chosen = [];
  if (host) { vals.host = host; if (on) on.host = true; chosen.push("host " + host); }
  if (port) { vals.port = port; if (on) on.port = true; chosen.push("port " + port); }
  if (key) { vals.api_key = key; if (on) on.api_key = true; chosen.push("APIキー設定済"); }
  if (!key && !String((p.values || {}).api_key || "").trim()) chosen.push("APIキーなし (認証なし)");
  const body = { name: p.name, backend: p.backend, values: vals, on,
    extra_args: p.extra_args || "", env: p.env || {}, cwd: p.cwd || "", command: p.command || "",
    custom_only: !!p.custom_only, note: p.note || "", simple_visible: !!p.simple_visible };
  const btn = $("#nt-go");
  btn.disabled = true;
  try {
    await api(`/api/profiles/${p.id}`, { method: "PUT", body });
    const cfg = W.cfgRendered || RUNCFG[W.backend] || {};
    // config-args (strata/maya) の host/port/key は CLI 上書きなので config 再生成は不要。
    // 設定先行フローの再生成はモデル紐付け後 (bindDownloadResult) に行う
    if (!W.preJob && cfg.regen && cfg.regen !== "config-args") {
      await api(`/api/profiles/${p.id}/${cfg.regen === "config-json" ? "config-json" : "launch-json"}`,
                { method: "POST" });
    }
    await refreshProf(p); // 再生成後の config 込み最新を後続ステップへ
    W.summary.push({ ok: true, t: "接続設定を保存しました (" + chosen.join(" · ") + ")" });
    if (W.preJob) { await startModelJob(); return; }
    renderCal(); go("s-cal");
  } catch (e) {
    err.textContent = "保存に失敗しました: " + e.message;
    err.classList.remove("hidden");
  } finally { btn.disabled = false; }
});
$("#nt-skip").addEventListener("click", async () => {
  if (W.preJob) { await startModelJob(); return; }
  renderCal(); go("s-cal");
});

/* ---------------------------------------------------------------- 2 実行: 設定の後のジョブ開始
   設定先行フローの终点: ② の選択 (dlReq / tableReq) + ③ の GPU / 並列数回答で
   DL / 純正セットアップジョブを開始する。GPU 指定はセットアップ自体に渡るので、
   エンジンビルド / 速度計測も選択カードで走る (他カードを空けておける)。 */
async function startModelJob() {
  if (!W.preJob || W.modelSkipped) { renderCal(); go("s-cal"); return; }
  // 開始済み (「もどる」で戻ってきた等): 二重 POST せず進捗 / 調整へ。
  // 対象はモデルジョブ (② の DL / モデル構成) だけ — ① インストールジョブ (done) を
  // 「モデルジョブ完了」と誤認してダウンロードをスキップしない (2026-11-16 実害:
  // 256G を選んでも POST されず調整画面へ飛んでいた)
  const mj = (W.job && W.job.id && (W.kind === "download" || W.job.phase === "model")) ? W.job : null;
  if (mj) {
    if (mj.status === "running" || mj.status === "queued") {
      if (W.kind === "download") { renderDownload(); go("s-download"); }
      else { renderProgress(); go("s-progress"); }
      startPoll(); return;
    }
    if (mj.status === "done") { renderCal(); go("s-cal"); return; }
  }
  const err = $("#nt-err");
  err.classList.add("hidden");
  try {
    let job;
    if (W.tableReq) {
      const isMaya = W.backend === "maya";
      const ep = isMaya ? "/api/installs/maya-model" : "/api/installs/strata-model";
      const body = { ...W.tableReq };
      const g = String(W.setupGpus || "").trim();
      if (g && g !== "auto") body.gpus = g;          // layer split 回答 (GLM / Qwen / MM3 GGUF / Strata)
      // Maya の並列数はセットアップに渡す (config の --parallel になり、DS4 / MM3 の
      // 推奨コンテキスト計算にも使われる)。Qwen は --parallel 非対応なので送らない
      if (isMaya && W.setupParallel > 0 && W.tableReq.family !== "qwen")
        body.parallel = W.setupParallel;
      job = await api(ep, { method: "POST", body });
      W.kind = "install";
    } else if (W.dlReq) {
      job = await api("/api/installs/download", { method: "POST", body: W.dlReq });
      W.kind = "download";
    } else { renderCal(); go("s-cal"); return; }
    W.job = job;
    W.logCursor = 0; W.logText = "";
    W.cancelAt = 0; W.cleanupShown = false;
    if (W.kind === "download") {
      renderDownload(); go("s-download");
    } else {
      setProgChrome("ステップ 2 / 5 — モデル", `${specName(W.backend)} がモデルを準備しています (時間がかかります)`);
      renderProgress(); go("s-progress");
    }
    startPoll();
  } catch (e) {
    // 開始失敗は接続設定画面に留まってやり直せる (設定は保存済み)
    err.textContent = "開始できませんでした: " + e.message;
    err.classList.remove("hidden");
  }
}

/* ---------------------------------------------------------------- ジョブ完了後の紐付け
   設定先行フロー: ③④ で作ったプロファイルに、ジョブの成果物 (config / モデル) を
   紐付け、③ で溜めた config 書き戻し (pendingUpdates) を適用する。 */
const profBody = (p, vals, on, name) => ({
  name: name || p.name, backend: p.backend, values: vals, on,
  extra_args: p.extra_args || "", env: p.env || {}, cwd: p.cwd || "", command: p.command || "",
  custom_only: !!p.custom_only, note: p.note || "", simple_visible: !!p.simple_visible });

/* basalt: HF ダウンロードの result.path はスナップショット DIR。--model は .basalt
   ファイル正体なので、一覧の include (既定 pack) かキャッシュスキャンでファイルへ
   解決する (解決できなければ DIR のまま = 起動時に正直なエラー) */
async function resolveBasaltPack(mp, repoId) {
  if (!mp || W.backend !== "basalt" || mp.endsWith(".basalt")) return mp;
  const ent = (W.mdList || []).find(m => m.repo_id === String(repoId || ""));
  const want = (ent && ent.include && ent.include[0]) || "";
  let files = [];
  try {
    const hm = await api("/api/hf-models?refresh=1");   // 取得直後でキャッシュが古い可能性
    const snap = (hm.models || []).find(m => m.snapshot === mp);
    files = ((snap && snap.basalt) || []).map(f => f.name);
  } catch { /* スキャン失敗は include で */ }
  const fname = files.includes(want) ? want : (files[0] || want);
  return fname ? mp.replace(/\/+$/, "") + "/" + fname : mp;
}

async function bindSetupResult(r) {
  /* Strata / Maya: ② セットアップジョブの config → プロファイルの --config。
     ③ の回答 (kv / tp / gpu) は config ができたここで書き戻す */
  try {
    const p = await refreshProf(W.calProfile || { id: W.cfgProfId });
    const vals = { ...(p.values || {}) };
    const on = p.on ? { ...p.on } : null;
    const cfgPath = String((r.result || {}).config || "").trim();
    if (cfgPath) { vals.config = cfgPath; if (on) on.config = true; }
    if (W.backend === "strata" && !String(vals.engine || "").trim()) {
      vals.engine = "strata"; if (on) on.engine = true;
    }
    await api(`/api/profiles/${p.id}`, { method: "PUT", body: profBody(p, vals, on) });
    const updates = W.pendingUpdates || {};
    if (Object.keys(updates).length)
      await api(`/api/profiles/${p.id}/config-args`, { method: "POST", body: { updates } });
    W.pendingUpdates = null;
    await refreshProf(p);
    W.summary.push({ ok: true, t: "モデル設定をプロファイルに紐付け、稼働設定を反映しました" });
  } catch (e) {
    W.summary.push({ ok: false, t: "モデル設定の紐付けに失敗しました: " + e.message + " (プロファイル編集から設定できます)" });
  }
}

async function bindDownloadResult(r) {
  /* 他バックエンド: DL の成果物 → プロファイルの model_path + 起動 JSON 再生成 */
  try {
    const p = await refreshProf(W.calProfile || { id: W.cfgProfId });
    const vals = { ...(p.values || {}) };
    const on = p.on ? { ...p.on } : null;
    const cfg = W.cfgRendered || RUNCFG[W.backend] || {};
    const modelKey = cfg.model === true ? "model_path" : (cfg.model || "model_path");
    let mp = String((r.result || {}).path || r.local_dir || "");
    mp = await resolveBasaltPack(mp, r.repo_id);
    if (mp) { vals[modelKey] = mp; if (on) on[modelKey] = true; }
    await api(`/api/profiles/${p.id}`, { method: "PUT", body: profBody(p, vals, on) });
    if (cfg.regen && cfg.regen !== "config-args") {
      await api(`/api/profiles/${p.id}/${cfg.regen === "config-json" ? "config-json" : "launch-json"}`,
                { method: "POST" });
    }
    await refreshProf(p);
    W.summary.push({ ok: true, t: "モデルをプロファイルに紐付けました" + (mp ? " (" + mp.split("/").pop() + ")" : "") });
  } catch (e) {
    W.summary.push({ ok: false, t: "モデルの紐付けに失敗しました: " + e.message + " (プロファイル編集から設定できます)" });
  }
}

/* ---------------------------------------------------------------- 3: 調整 */
function calPick(profs) {
  const mine = W.calProfile && profs.find(x => x.id === W.calProfile.id);
  if (mine) return mine;
  return profs.slice().sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))[0] || null;
}
async function renderCal() {
  const runOpt = $$("#ca-list input").find(i => i.value === "run");
  const canCal = W.me && (W.me.role === "admin" || !W.me.auth_enabled || (W.me.perms || {}).calibrate);
  let ok = false;
  try {
    const [c, st] = await Promise.all([api("/api/calibrations").catch(() => null),
                                       api("/api/state").catch(() => null)]);
    const supported = (c && c.supported) || [];
    // このバックエンドに調整が無い (basalt 等) とき、他バックエンドのプロファイルを
    // 巻き込んで実行しないよう対象をこのバックエンドに限定する。
    // calOff は API が応答したときだけ判定する (403 / 取得失敗を「非対応」と誤読しない)
    const calOff = !!c && !supported.includes(W.backend);
    const calRunning = !!(c && c.active);
    const profs = ((st && st.profiles) || []).filter(p => supported.includes(p.backend)
      && p.backend === W.backend);
    ok = canCal && !calOff && !calRunning && !W.busyInstall && profs.length > 0;
    W.calProfiles = profs;
    /* ⑤ の対象は ③ で作り/編集したプロファイル (W.calProfile)。re-load で
       W.calProfile を失ったときは「最近更新した自バックエンドのプロファイル」に
       倒す — 一覧先頭 (最古) を選んで他人のプロファイルを触る事故防止
       (2026-11-09: 旧プロファイル p_6acf49a3ad87 を掴んで config 整合チェックに弾かれた) */
    W.calPick = calPick(profs);
    if (canCal && calOff) {   // 調整が無いバックエンド (basalt 等) は調整画面を出さずに完了へ
      W.summary.push({ ok: true, t: "このバックエンドはキャリブレーション非対応のため、調整はスキップしました" });
      renderDone(); go("s-done"); return;
    }
    $("#ca-time").textContent = (profs.find(p => (p.backend || "").includes("freetoken"))
      ? "FreeToken は目安 3〜4 時間かかります"
      : W.backend === "maya" ? "Maya: GLM 約 10〜15 分 / DeepSeek 約 35〜70 分 (TP 調整 + drafter 速度計測)"
      : "目安: 10〜30 分 (バックエンドにより変わります)");
    if (!canCal) $("#ca-sub").textContent = "調整はキャリブレーション権限のあるアカウントで実行できます。ダッシュボードの「キャリブレーション」から後で実行できます。";
    else if (calRunning) $("#ca-sub").textContent = "別のキャリブレーションが実行中です (同時に 1 件)。完了後に開始できます。";
    else if (W.busyInstall) $("#ca-sub").textContent = "セットアップ (インストール / ダウンロード) ジョブが実行中です。完了後に調整を開始できます。";
    else if (!c) $("#ca-sub").textContent = "キャリブレーション情報を取得できませんでした。ダッシュボードの「キャリブレーション」から後で実行できます。";
    else if (!profs.length) $("#ca-sub").textContent = "調整対象のプロファイルがまだありません。プロファイルを作成してから、ダッシュボードの「キャリブレーション」で実行してください。";
    else {
      let sub = "調整 (キャリブレーション) はこの機械向けの性能調整です。実行中は GPU を独占します。"
        + "対象: " + (W.calPick ? W.calPick.name : "(未選択)");
      /* Maya: drafter の速度計測もここに含まれる (セットアップでは計測しない)。
         スキップすると drafter なしで起動する — 正直に予告する */
      const cn = String(((W.calPick || {}).values || {}).config || "").split("/").pop();
      if (W.backend === "maya" && /^maya-(ds4|mm3|minimax|cyber-frost|qwen)/.test(cn))
        sub += " drafter (DSpark / MTP) の速度計測もここで行います。スキップすると drafter なしで起動します (後から 🎯 で実行可)。";
      $("#ca-sub").textContent = sub;
    }
  } catch { ok = false; }
  runOpt.disabled = !ok;
  $$("#ca-list .opt").forEach(o => { if (o.contains(runOpt)) o.style.opacity = ok ? "" : ".45"; });
  if (!ok) $$("#ca-list input").find(i => i.value === "skip").checked = true;
  syncCalSel();
}
function syncCalSel() {
  $$("#ca-list .opt").forEach(o => {
    const i = o.querySelector("input"); o.classList.toggle("sel", i.checked);
  });
  $("#ca-go").textContent = $$("#ca-list input").find(i => i.checked)?.value === "run" ? "調整を開始" : "設定を完了する";
}
$$("#ca-list input").forEach(i => i.addEventListener("change", syncCalSel));
$("#ca-go").addEventListener("click", async () => {
  const run = $$("#ca-list input").find(i => i.checked)?.value === "run";
  if (!run) {
    W.summary.push({ ok: true, t: "調整 (キャリブレーション) は実行しませんでした — ダッシュボードからいつでも実行できます" });
    renderDone(); go("s-done"); return;
  }
  const profs = W.calProfiles || [];
  /* 対象は renderCal が決めた W.calPick (③ で作ったプロファイル優先)。
     profs[0] = 一覧の最古プロファイルで、Strata 側保存との config 整合チェックに
     弾かれるか別設定を調整してしまう (2026-11-09 の事故) */
  const p = W.calPick || calPick(profs) || profs[0];
  if (!p) { W.summary.push({ ok: false, t: "調整対象のプロファイルがなく、スキップしました" }); renderDone(); go("s-done"); return; }
  try {
    const job = await api("/api/calibrations", { method: "POST", body: { profile_id: p.id, restart_after: false } });
    W.kind = "cal"; W.job = job; W.logCursor = 0; W.logText = "";
    setProgChrome(calKicker(), "この PC に調整しています…");
    $("#pg-next").classList.add("hidden");
    renderProgress(); go("s-progress"); startPoll();
  } catch (e) {
    alert("調整を開始できませんでした: " + e.message);
    renderDone(); go("s-done");
  }
});

/* ---------------------------------------------------------------- 完了 */
function renderDone() {
  $("#dn-sum").innerHTML = (W.summary.length ? W.summary : [{ ok: true, t: "変更はありません" }])
    .map(s => `<li${s.ok ? "" : ' class="skip"'}><span class="ic">${s.ok ? "✓" : "–"}</span>${esc(s.t)}</li>`).join("");
}

/* ---------------------------------------------------------------- ポーリング */
function startPoll() { stopPoll(); W.timer = setInterval(pollTick, 2000); pollTick(); }
function stopPoll() { if (W.timer) { clearInterval(W.timer); W.timer = null; } }

boot();
