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
                    model: "モデルを準備 (Strata)", verify: "動作確認 (import)" };
const CAL_STEPS = { prepare: "調整の準備", auto: "最適構成を自動で探す", lookup: "キャッシュを引く",
                    sweep: "実際に測る (実測)", create: "プロファイルを更新", adjust: "調整を反映",
                    restart: "サーバーを起動し直す" };
const STEP_NAMES = { queued: "待機しています", running: "", done: "完了", failed: "失敗",
                     cancelled: "中断しました", lost: "中断されました (サーバー再起動)" };
const bytes = n => { n = n || 0;
  return n >= 2**30 ? (n / 2**30).toFixed(2) + " GiB"
       : n >= 2**20 ? (n / 2**20).toFixed(1) + " MB" : Math.round(n / 1024) + " KiB"; };

const W = {
  me: null, installs: { jobs: [], installable: [], tool: "uv", cacheDir: "" },
  backend: "", ref: "", clean: false,
  kind: "", job: null, timer: null, logCursor: 0, logText: "",
  ema: null, tLast: 0, dLast: 0, growT: 0,
  cancelAt: 0,            // 中断要求を出した時刻 (強制停止ボタンの表示判定)
  cleanupShown: false,    // 中断残骸の削除画面は 1 ジョブ 1 回だけ出す
  cfgSkip: true, calProfile: null,
  cfgProfId: null, cfgProfNew: false, backends: null,
  summary: [],
};
const spec = id => (W.installs.installable || []).find(e => e.id === (id ?? W.backend)) || null;
const specName = id => (spec(id) || {}).name || id || "";
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
function goBack() {
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
  if (!admin && !(W.me.perms || {}).install) { go("s-perm"); return; }
  let inst = null;
  try {
    const pair = await Promise.all([api("/api/installs").catch(() => null),
                                    api("/api/calibrations").catch(() => null)]);
    inst = pair[0];
    W.activeInstalls = pair[0] && pair[0].active;
    W.activeCal = pair[1] && pair[1].active;
  } catch { /* 下で inst を見て判定 */ }
  if (!inst) { go(W.me.auth_enabled ? "s-login" : "s-welcome"); return; }
  W.installs = { jobs: inst.jobs || [], installable: inst.installable || [], tool: inst.tool || "uv", cacheDir: inst.cache_dir || "" };
  const pre = new URLSearchParams(location.search).get("backend");
  if (pre && spec(pre)) W.backend = pre;

  /* 実行中のジョブがあればその画面へ素直に戻る (リロード対策)。cal は長時間走るので必須 */
  const live = r => r && (r.status === "running" || r.status === "queued");
  const act = W.installs.jobs.find(r => r.id === W.activeInstalls);
  if (live(act)) {
    W.kind = act.kind === "download" ? "download" : "install";
    W.job = act; W.backend = act.backend || W.backend;
    if (W.kind === "download") { renderDownload(); go("s-download"); }
    else { renderProgress(); go("s-progress"); }
    startPoll();
    return;
  }
  if (W.activeCal) {
    try {
      const cal = await api("/api/calibrations/" + W.activeCal);
      if (live(cal)) {
        W.kind = "cal"; W.job = cal;
        setProgChrome("ステップ 5 / 5 — 調整 (任意)", "この PC に調整しています…");
        renderProgress(); go("s-progress"); startPoll();
        return;
      }
    } catch { /* 取得失敗時は通常の導入導線へ */ }
  }
  renderBackends();
  if (pre && spec(pre)) go("s-backend"); else go("s-welcome");
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
  $("#be-next").disabled = !spec();
  $("#be-next").dataset.go = "";
}
$("#be-next").addEventListener("click", () => {
  const sp = spec(); if (!sp) return;
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
      + `3. 依存パッケージを導入 (${sp.requirements ? `${toolLabel(sp)} -r ${esc(sp.requirements)}` : esc(toolLabel(sp))})`
      + (sp.cmake ? "<br>4. ネイティブエンジンをビルド (cmake → bin/basalt)" : "")
      + (sp.engine_fetch ? "<br>4. ネイティブエンジンを受信 (公式リリース → engine/strata)" : "")],
    ["この後の予定", doing],
  ];
  $("#rv-rows").innerHTML = rows.map(([k, v]) =>
    `<div class="row-item"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  $("#rv-preview").textContent = `git clone → .venv → `
    + (sp.requirements ? `${toolLabel(sp)} -r ${sp.requirements}` : `${toolLabel(sp)} -e ".[${sp.extras || "accel"}]"`)
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
           : r.status === "running" ? "Strata がモデルを準備しています (時間がかかります)"
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
    finishJob();
  }
}
/* インストール完了後の追い書き */
const INSTALL_NOTES = {
  strata: "インストール完了です。次はモデルの設定 — 家系・サイズ・コンテキスト長を選ぶと、"
    + "Strata 本体のセットアップ手順 (ダウンロードと strata-<model>.json 作成) がこの画面のまま"
    + "走りきるため、シェルの準備はありません。",
};
function finishJob() {
  const r = W.job || {};
  if (W.kind === "install") {
    if (r.phase === "model") {
      // Strata ② のモデル構成ジョブ: 完了後は稼働設定 (config 接続) へ直行する
      if (r.status === "done") {
        W.summary.push({ ok: true, t: "Strata のモデル設定が完了しました (strata-<model>.json 作成済み)" });
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
      $("#dl-next").classList.remove("hidden");
      $("#dl-cancel").style.display = "none";
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
/* ---- Strata: 純正 setup.py を --yes (非対話) で走らせる選択面板 ---------------- */
const MD_LABELS = {
  other: [["モデルをダウンロードする", "対応モデルの一覧から選んで取得します"],
          ["ダウンロード済みのモデルを使う", "この PC にあるモデルから選びます (再取得しません)"]],
  strata: [["モデルをダウンロードする", "家系・サイズ・コンテキスト長を選ぶと、Strata 本体のセットアップ手順でダウンロードと設定作成まで走ります"],
           ["ダウンロード済みのモデルを使う", "この PC に重みが在るモデルから選びます (再取得しません)"]],
};
function setMdModeLabels(isSt) {
  const L = isSt ? MD_LABELS.strata : MD_LABELS.other;
  $("#md-dl-mode-t1").textContent = L[0][0]; $("#md-dl-mode-t2").textContent = L[0][1];
  $("#md-local-mode-t1").textContent = L[1][0]; $("#md-local-mode-t2").textContent = L[1][1];
  // Strata はモデル設定なしでは起動設定 (values.config) が作れないので「あとでする」を隠す
  $("#md-later").classList.toggle("hidden", !!isSt);
}
async function loadStrata() {
  if (W.mdStrata) return W.mdStrata;
  try { W.mdStrata = await api("/api/installs/strata-model"); }
  catch { W.mdStrata = { available: false, families: {}, contexts: [], configs: [], venv_ready: false }; }
  return W.mdStrata;
}
function mdStModels() {
  return (((W.mdStrata || {}).families || {})[W.mdStFam] || {}).models || {};
}
function mdStFull(m) {
  const h = (mdStModels()[m] || {}).have || {};
  return !!(h.files && h.found >= h.files);
}
function mdStFillSizes() {
  const st = W.mdStrata || {};
  const ms = mdStModels();
  const ram = (st.pc || {}).ram_gb || 0;
  const ids = Object.keys(ms);
  // 推奨 = この PC の RAM に収まる最大サイズ (無ければ最小)。selection は推奨から始める
  const fit = ids.filter(m => !(Number(ms[m].ram_gb) > ram))
                 .sort((a, b) => (ms[b].ram_gb || 0) - (ms[a].ram_gb || 0));
  const rec = fit[0] || ids[0] || "";
  // ダウンロード択の一覧は「まだ無いモデル」から選ぶ — 済みのものはグレー表示 (選択不可)
  if (!ids.includes(W.mdStModel) || mdStFull(W.mdStModel)) {
    W.mdStModel = fit.find(m => !mdStFull(m)) || rec || "";
  }
  $("#md-size").innerHTML = ids.map(m => {
    const d = ms[m] || {}; const bits = [];
    if (d.ram_gb) bits.push(`RAM ${d.ram_gb} GB${ram && d.ram_gb > ram ? " ※この PC " + ram + " GB" : ""}`);
    if (!mdStFull(m) && d.download_gb) bits.push(`DL ${d.download_gb} GB`);
    return `<option value="${esc(m)}"${m === W.mdStModel ? " selected" : ""}${mdStFull(m) ? " disabled" : ""}>`
      + `${esc((d.label || m) + (mdStFull(m) ? " (ダウンロード済み" + (((d.have || {}).gb) ? " " + d.have.gb + " GB" : "") + ")" : "") + (bits.length ? " — " + bits.join(" / ") : "") + (m === rec && !mdStFull(m) ? " (この PC 推奨)" : ""))}</option>`;
  }).join("") || '<option value="">(サイズ一覧が空です)</option>';
  // vision は対応パックだけ選択を出して既定 ON。非対応は送信しない
  const d = ms[W.mdStModel] || {};
  $("#md-vision-opt").style.display = d.vision ? "" : "none";
  $("#md-vision-opt").classList.toggle("sel", $("#md-vision").checked);
  mdStHint();
}
function mdStRecCtx() {
  const d = mdStModels()[W.mdStModel] || {};
  return Number(d.rec_context) || 131072;
}
function mdStFillCtx() {
  const st = W.mdStrata || {};
  const fam = (st.families || {})[W.mdStFam] || {};
  const maxc = Number(fam.max_context) || 524288;
  const cs = ((st.contexts || []).length ? st.contexts : [8192, 32768, 65536, 131072, 204800, 262144])
    .filter(c => c <= maxc);
  // 既定は純正の推奨 (own_path_context / ram_ctx)。家族・サイズを変えたら推奨へ戻る
  const rec = mdStRecCtx();
  if (!cs.includes(W.mdStCtx) || W.mdStCtxRec !== rec) W.mdStCtx = cs.includes(rec) ? rec : 131072;
  W.mdStCtxRec = rec;
  $("#md-ctx").innerHTML = cs.map(c => `<option value="${c}"${c === W.mdStCtx ? " selected" : ""}>`
    + `${c >= 1024 ? Math.round(c / 1024) + "K" : c}${c === rec ? " (推奨)" : c > rec ? " (大)" : ""}</option>`).join("");
  mdStHint();   // 推奨と選択の差はヒントに出す (サイズ変更後は ctx が後から変わるためここで再計算)
}
function mdStHint() {
  const st = W.mdStrata || {};
  const d = mdStModels()[W.mdStModel] || {};
  const pc = st.pc || {}; const parts = [];
  if (!st.available) parts.push("Strata のモデル一覧が読めません — 前に戻って ① インストールを先に実行してください");
  else if (!st.venv_ready) parts.push("Strata の実行環境 (.venv) がまだありません — 前に戻って ① インストールを実行してください");
  if (d.about) parts.push(d.about);
  const h = d.have || {};
  if (h.files && h.found >= h.files) {
    parts.push("重みはこの PC に在ります — setup.py が検証してダウンロードを素通りし、"
      + "config JSON の作成だけ走るので速いです");
  } else if (h.found) {
    parts.push(`重みが部分的です (${h.found}/${h.files}) — 足りない分は受信します`);
  } else if (d.download_gb) {
    parts.push(`ダウンロード ${d.download_gb} GB`);
  }
  if (d.ram_gb) parts.push(`RAM ${d.ram_gb} GB 推奨`);
  if (pc.disk_free_gb) parts.push(`モデル保存先 (${pc.data_dir || "Strata-data"}) の空き ${pc.disk_free_gb} GB`);
  /* 純正の対話と同じ立場: 推奨より大きいコンテキストは「注記して従う」。
     24GB クラスで 512K はエンジン起動時の KV 確保に失敗しうる (2026-11-09 実測) */
  const rec = mdStRecCtx();
  if (W.mdStCtx > rec) {
    parts.push(`※ コンテキスト ${Math.round(W.mdStCtx / 1024)}K はこの PC 向け推奨 ${Math.round(rec / 1024)}K より大きい — 起動時に VRAM/RAM 不足で失敗することがあります`);
  }
  $("#md-strata-hint").textContent = parts.join(" ・ ");
}
function mdSync() {
  const mode = mdMode();
  $$("#md-modes .opt").forEach(o => {
    const i = o.querySelector("input"); o.classList.toggle("sel", i.checked); });
  if (W.backend === "strata") {
    /* 2 択は他バックエンドと同じ形: ダウンロード択 (済みのサイズはグレー) /
       ダウンロード済みのモデルを使う (モデル名で並べ、JSON は一切見せない) */
    $("#md-adv").hidden = true;
    $("#md-repo").value = "";                        // repo id 直接入力は Strata では意味が無い
    ["#md-dl-field", "#md-local-field", "#md-speed-field"].forEach(s => $(s).classList.add("hidden"));
    $("#md-strata").classList.toggle("hidden", mode !== "dl");
    $("#md-strata-exist-field").classList.toggle("hidden", mode !== "local");
    const btn = $("#md-start");
    const st = W.mdStrata || {};
    if (mode === "dl") {
      btn.textContent = "モデル設定を開始";
      btn.disabled = !st.available || !st.venv_ready || !W.mdStFam || !W.mdStModel;
      $("#md-dest").textContent = "ダウンロードと設定は Strata 本体の手順が走ります — 中断しても続きから再開できます";
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
  setMdModeLabels(false);
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
    btn.textContent = "ダウンロード";
    btn.disabled = !$("#md-repo").value.trim() && !$("#md-sel").value;
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
async function renderModel() {
  if (W.backend === "strata") { await renderModelStrata(); return; }
  setMdModeLabels(false);
  const sp = spec();
  const ms = (sp && sp.supported_models) || [];
  W.mdList = ms;                                    // クリック時に include (取得ファイル) を引く
  try {
    const hm = await api("/api/hf-models");
    /* 選択中バックエンドが読める形のモデルだけに出す
       (basalt = .basalt pack / llama.cpp = gguf / 他は重量物ディレクトリ) */
    const shape = W.backend === "basalt" ? "basalt" : (W.backend === "llamacpp" ? "gguf" : "");
    mdLocal = (hm.models || []).filter(m => m.repo_id && (
      shape === "basalt" ? (m.basalt || []).length
      : shape === "gguf" ? ((m.gguf || []).length || m.has_weights)
      : m.has_weights));
  } catch { mdLocal = []; }
  $("#md-sel").innerHTML = ms.length
    ? ms.map((m, i) => `<option value="${esc(m.repo_id)}"${i === 0 ? " selected" : ""}>`
        + `${esc(m.repo_id)}${m.name ? " — " + esc(m.name) : ""}</option>`).join("")
    : '<option value="">(このバックエンドに対応モデル一覧がありません)</option>';
  $("#md-local").innerHTML = mdLocal.length
    ? mdLocal.map((m, i) => `<option value="${esc(m.repo_id)}"${i === 0 ? " selected" : ""}>`
        + `${esc(m.repo_id)}` + ((m.basalt || []).length ? ` · ${esc(m.basalt[0].name)}` : "")
        + `${m.source === "data" ? " — 導入済みモデルフォルダ" : ""}</option>`).join("")
    : '<option value="">(このバックエンドで使えるダウンロード済みモデルが見つかりません)</option>';
  /* 済みモデルが 0 件ならその選択を不可にしてダウンロード択に寄せる */
  const localR = $$("#md-modes input").find(i => i.value === "local");
  localR.disabled = !mdLocal.length;
  $$("#md-modes .opt").forEach(o => { if (o.contains(localR)) o.style.opacity = mdLocal.length ? "" : ".45"; });
  if (localR.disabled && mdMode() === "local") {
    $$("#md-modes input").find(i => i.value === "dl").checked = true;
  }
  $("#md-err").classList.add("hidden");
  $("#md-sub").textContent = ms.length
    ? "新しいモデルをダウンロードするか、この PC ですでにダウンロード済みのモデルを使うかを選んでください。"
    : "このバックエンドには対応モデル一覧がないため、「その他: repo id を直接入力する」から選ぶか、ダウンロード済みのモデルを使ってください。";
  mdSync();
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
async function renderModelStrata() {
  setMdModeLabels(true);
  W.mdStrata = null;          // 再入時は表も設定済み一覧も最新の取り直し (サーバー側に 60s キャッシュ)
  const st = await loadStrata();
  const fams = st.families || {};
  const famIds = Object.keys(fams).filter(f => Object.keys((fams[f] || {}).models || {}).length);
  if (!famIds.includes(W.mdStFam)) W.mdStFam = famIds.includes("qwen") ? "qwen" : (famIds[0] || "");
  $("#md-family").innerHTML = famIds.map(f => `<option value="${esc(f)}"${f === W.mdStFam ? " selected" : ""}>`
    + `${esc(fams[f].title || f)}</option>`).join("")
    || '<option value="">(Strata のモデル一覧が読めません)</option>';
  mdStFillSizes(); mdStFillCtx();
  /* ダウンロード済み一覧: 表から作る (モデル名 + サイズ + GB)。JSON 名は内部では
     使うが UI には出さない — 「そのモデルが在る」だけがユーザーの情報 */
  const dl = [];
  for (const fid of famIds) {
    const f = fams[fid];
    for (const [m, d] of Object.entries(f.models || {})) {
      if (mdStFullIn(f, m)) dl.push({ key: fid + "/" + m, fam: fid, model: m,
                                      label: (f.title || fid) + " · " + (d.label || m),
                                      gb: (d.have || {}).gb });
    }
  }
  st.downloaded = dl;
  $("#md-strata-exist").innerHTML = dl.map((x, i) => `<option value="${esc(x.key)}"${i === 0 ? " selected" : ""}>`
    + `${esc(x.label + " — " + (x.gb || "?") + " GB")}</option>`).join("")
    || '<option value="">(ダウンロード済みのモデルがありません)</option>';
  const localR = $$("#md-modes input").find(i => i.value === "local");
  localR.disabled = !dl.length;
  $$("#md-modes .opt").forEach(o => { if (o.contains(localR)) o.style.opacity = dl.length ? "" : ".45"; });
  if (localR.disabled && mdMode() === "local") {
    $$("#md-modes input").find(i => i.value === "dl").checked = true;
  }
  $("#md-err").classList.add("hidden");
  $("#md-sub").textContent = "Strata は家系・サイズ・コンテキスト長を選ぶだけです。Strata 本体のセットアップ手順がこのまま走り、"
    + "ダウンロードから起動設定 (strata-<model>.json) の作成まで完了します。ダウンロード済みのモデルは"
    + "「ダウンロード済みのモデルを使う」から再取得なしで選べます。";
  mdSync();
}
function mdStFullIn(f, m) {
  const h = ((f || {}).models || {})[m] || {};
  const hv = h.have || {};
  return !!(hv.files && hv.found >= hv.files);
}
/* ダウンロードの有無に関わらず同じ 1 経路: 純正 setup.py (再開対応・SHA-256 検証・
   既存ファイルは検証して素通り) をジョブとして走らせる。config JSON はその成果物 */
async function mdStartStrata(btn) {
  const st = W.mdStrata || {};
  if (mdMode() === "local") {
    const x = (st.downloaded || []).find(v => v.key === $("#md-strata-exist").value);
    if (!x) return;
    /* config JSON は内部の成果物 — 純正命名作法の config が在って重み ok ならそのまま採用、
       重みが在るのに config が無い (削除/別経路) ときは同じジョブを走らせるだけで
       setup.py が検証・受信素通り・config 作成をする (ダウンロードは起きない) */
    const f = (st.families || {})[x.fam] || {};
    const key = "strata-" + String((f.tag || "") + x.model).toLowerCase() + ".json";
    const c = (st.configs || []).find(y => y.name === key && (y.weights || {}).ok === true);
    if (c) {
      W.kind = "install";
      W.job = { id: "", kind: "install", phase: "model", status: "done", backend: "strata",
                repo_id: "", config_name: c.name, model: x.fam + "/" + x.model,
                result: { config: c.file } };
      W.cfgProfId = null; W.cfgProfNew = false;
      W.summary.push({ ok: true, t: `ダウンロード済みの ${x.label} を使用します` });
      renderConfig(); go("s-config");
      return;
    }
    btn.disabled = true; btn.textContent = "開始中…";
    try {
      const d = (f.models || {})[x.model] || {};
      const job = await api("/api/installs/strata-model", { method: "POST",
        body: { family: x.fam, model: x.model, context: d.rec_context || 131072, vision: "" } });
      W.kind = "install"; W.job = job;
      W.logCursor = 0; W.logText = "";
      W.cfgProfId = null; W.cfgProfNew = false;
      renderProgress(); go("s-progress"); startPoll();
    } catch (e) {
      $("#md-err").textContent = "開始できませんでした: " + e.message;
      $("#md-err").classList.remove("hidden");
    } finally { btn.textContent = "このモデルで続ける"; mdSync(); }
    return;
  }
  btn.disabled = true; btn.textContent = "開始中…";
  try {
    /* vision ON = --vision を渡さない = 純正のマシン既定 (own-path 系は VRAM が
       24GB 未満なら CPU エンコーダ、以上で GPU エンコーダ。2026-11-09: 24.4GB 未満
       のこの PC で --vision gpu を送るのは推奨外)。OFF = 明示の "no" */
    const vision = ($("#md-vision-opt").style.display === "none") ? ""
                 : ($("#md-vision").checked ? "" : "no");
    const job = await api("/api/installs/strata-model", { method: "POST",
      body: { family: W.mdStFam, model: W.mdStModel,
              context: parseInt($("#md-ctx").value, 10) || 131072, vision } });
    W.kind = "install"; W.job = job;
    W.logCursor = 0; W.logText = "";
    W.cfgProfId = null; W.cfgProfNew = false;
    renderProgress(); go("s-progress"); startPoll();
  } catch (e) {
    $("#md-err").textContent = "開始できませんでした: " + e.message;
    $("#md-err").classList.remove("hidden");
  } finally {
    btn.textContent = "モデル設定を開始"; mdSync();
  }
}
$("#md-start").addEventListener("click", async () => {
  const btn = $("#md-start");
  $("#md-err").classList.add("hidden");
  if (W.backend === "strata") { await mdStartStrata(btn); return; }
  if (mdMode() === "local") {
    /* 済みモデルは「完了済みのダウンロードジョブ」に化かして後続 (稼働設定の
       プロファイル名 / model_path 解決) をそのまま通す */
    const m = mdLocal.find(x => x.repo_id === $("#md-local").value);
    if (!m) return;
    W.kind = "download";
    W.job = { id: "", kind: "download", status: "done", backend: W.backend,
              repo_id: m.repo_id, result: { path: m.snapshot }, local_dir: m.snapshot };
    W.cfgProfId = null; W.cfgProfNew = false;
    W.summary.push({ ok: true, t: `ダウンロード済みのモデル ${m.repo_id} を使用します` });
    renderConfig(); go("s-config");
    return;
  }
  const repo = $("#md-repo").value.trim() || $("#md-sel").value;
  if (!repo) return;
  /* 一覧から選んだモデルは既定ファイル (pack 1 個) だけ取得する。repo 全体 (1 TB) を
     落とさないための allow_patterns。repo id を手入力したときは指定なし = 全体 */
  const entry = (W.mdList || []).find(m => m.repo_id === repo) || null;
  const patterns = (entry && entry.include) || [];
  const sp = spec();
  btn.disabled = true; btn.textContent = "開始中…";
  try {
    const kb = parseInt($("#md-speed").value, 10) || 0;
    const job = await api("/api/installs/download", { method: "POST",
      body: { repo_id: repo, revision: "", backend: (sp && sp.model_data) ? sp.id : "",
              allow_patterns: patterns, max_kbps: kb } });
    W.kind = "download"; W.job = job;
    W.cancelAt = 0; W.cleanupShown = false;   // 中断/掃除の状態を新しいジョブ用にリセット
    // 新しいセットアップ (モデル取得) を始めたので、稼働設定用の
    // プロファイルは作り直しになる
    W.cfgProfId = null; W.cfgProfNew = false;
    renderDownload(); go("s-download"); startPoll();
  } catch (e) {
    $("#md-err").textContent = "開始できませんでした: " + e.message;
    $("#md-err").classList.remove("hidden");
  } finally {
    btn.textContent = "ダウンロード"; mdSync();
  }
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
   (見出し下の 1 行 sec.hint に集約する)。行は [値, 表示, 推奨タグ] の 3 つ組。 */
const CF_TOK = v => { const n = Math.round(Number(v));
  if (!Number.isFinite(n)) return String(v);
  if (n >= 1048576 && n % 1048576 === 0) return `${Math.round(n / 1048576)}M`;
  return (n >= 1024 && n % 1024 === 0) ? `${Math.round(n / 1024)}K` : n.toLocaleString(); };
const CF_NUM = v => { const n = parseInt(v, 10); return Number.isFinite(n) ? String(n) : String(v); };
const CF_CTX = [["32768", "32K", ""], ["131072", "128K", "推奨"], ["262144", "256K", ""]];
const CF_KV = [["fp8", "fp8", "推奨"], ["bf16", "bf16", ""], ["auto", "auto", ""]];
const CF_PAR = [["1", "1", ""], ["2", "2", ""], ["4", "4", "推奨"], ["8", "8", ""]];
const CF_TP = [["1", "1", ""], ["2", "2", ""], ["4", "4", ""], ["8", "8", ""]];
/* perUser 項目 (FreeToken の一人あたり設定): 画面は 1 人あたりで聞き、JSON へは
   合計 = 1人あたり × 並列数 を書く。--num-tokens は全員で共有する KV 総量なので、
   1人256K・4並列なら 1M を書かないと 1 人あたり 64K しか残らない。 */
function cfParNow() {
  const el = document.querySelector("#cf-par");
  const n = parseInt(el && el.value, 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}
/* 見出し下の補足 1 行。perUser は「1人あたり × 並列 = 合計KV」を生きた値で示す */
function cfSyncHints() {
  const cfg = RUNCFG[W.backend] || {};
  for (const id of ["ctx", "kv", "tp", "par", "vis"]) {
    const sec = cfg[id]; const hint = document.querySelector(`#cf-${id}-hint`);
    if (!sec || !hint) continue;
    hint.textContent = sec.hint || "";
    if (!sec.perUser) continue;
    const el = document.querySelector(`#cf-${id}`);
    const n = parseInt(el ? el.value : "", 10);
    if (!Number.isFinite(n) || n <= 0) continue;
    const par = cfParNow();
    const per = el.selectedOptions[0] && el.selectedOptions[0].dataset.raw === "1"
      ? Math.round(n / par) : n;
    hint.innerHTML = esc(sec.hint)
      + `<br>1人あたり ${esc(CF_TOK(per))} × ${par}並列 = KV合計 ${esc(CF_TOK(per * par))}`;
  }
}
const RUNCFG = {
  "freetoken-fork": { regen: true, model: true,
    ctx: { key: "num_tokens", label: "一人当たりのコンテキスト長", short: "1人あたり", perUser: true,
           hint: "長いほどVRAMを消費します。調整はこの長さ前提のKVキャッシュで行います", opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式",
          hint: "会話履歴の格納精度です。fp8はメモリ半減、bf16は品質優先", opts: CF_KV, cur: v => v },
    tp: { key: "tp_size", label: "Tensor Parallel (GPU分割数)", short: "GPU分割",
          hint: "モデルを何枚のGPUに分割するか。VRAMが足りない場合は増やします", opts: CF_TP, cur: CF_NUM },
    par: { key: "max_running_requests", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: CF_NUM } },
  "sglang": { regen: false,
    ctx: { key: "context_length", label: "コンテキスト長",
           hint: "長いほどVRAMを消費します", opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式",
          hint: "fp8はメモリ半減、bf16は品質優先", opts: CF_KV, cur: v => v },
    par: { key: "max_running_requests", label: "並列推論数",
           hint: "同時に処理するリクエスト数", opts: CF_PAR, cur: CF_NUM } },
  "vllm": { regen: false,
    ctx: { key: "max_model_len", label: "コンテキスト長",
           hint: "長いほどVRAMを消費します", opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式",
          hint: "fp8はメモリ半減、bf16は品質優先", opts: CF_KV, cur: v => v },
    par: { key: "max_num_seqs", label: "並列推論数",
           hint: "同時に処理するリクエスト数", opts: CF_PAR, cur: CF_NUM } },
  /* strata: values.config は ② のモデル構成 (純正 setup.py) が作った strata-<model>.json。
     コンテキスト長などはその config の args に入るので、ここでは並列数だけ扱う */
  "strata": { regen: false, model: "config",
    par: { key: "parallel", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: CF_NUM } },
  "maya": { regen: false,
    par: { key: "parallel", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: CF_NUM } },
  /* basalt: コンテキスト / KV / 並列は設定 JSON (models[]) が正。保存で自動再生成する。
     調整 (キャリブレーション) は無いので s-cal は自動でスキップされる */
  "basalt": { regen: "config-json", model: "model",
    ctx: { key: "max_context", label: "コンテキスト長",
           hint: "プロンプト+生成で共有します。並列数を増やすと 1 スロットあたりは コンテキスト÷並列 になります",
           opts: [["8192", "8K", "既定"], ["32768", "32K", "推奨"],
                  ["131072", "128K", ""], ["262144", "256K", ""]], cur: CF_TOK },
    kv: { key: "kv", label: "KVキャッシュ形式",
          hint: "fp16=品質優先 / int8=VRAM半減 / q4_0・k8v4=さらに圧縮",
          opts: [["fp16", "fp16", "既定"], ["int8", "int8", "推奨"],
                 ["q4_0", "q4_0", ""], ["k8v4", "k8v4", ""]], cur: v => v },
    par: { key: "parallel", label: "並列推論数",
           hint: "同時に処理するリクエスト数 (1-8)。コンテキスト長はこの数で分割されます",
           opts: CF_PAR, cur: CF_NUM },
    vis: { key: "vision", label: "画像入力 (ビジョン)",
           hint: "dynamic=エンジン内で読む (推奨・常駐VRAMなし)。off にすると画像リクエストはエラーになります",
           opts: [["dynamic", "dynamic", "推奨"], ["off", "off", ""], ["cpu", "cpu", ""]], cur: v => v } },
};
async function renderConfig() {
  W.cfgSkip = true;
  const cfg = RUNCFG[W.backend];
  let p = null;
  try {
    const j = W.job;
    const profs = await api("/api/profiles");
    if (W.cfgProfId) {
      // 戻る/次への往復では、このセットアップで作ったプロファイルを再利用
      p = (profs || []).find(x => x.id === W.cfgProfId) || null;
    } else if (cfg) {
      // セットアップのたびに専用プロファイルを新規作成する。
      // 既存 (前回のセットアップで作ったものを含む) を再利用すると、その
      // プロファイルの設定 (コンテキスト長 / 並列など) と JSON を上書きして
      // しまい、設定違いのプロファイルを増やせなくなる。
      const nm = (j && j.config_name) ? String(j.config_name).replace(/^strata-/, "").replace(/\.json$/, "")
                 : (j && j.repo_id) ? String(j.repo_id).split("/").pop()
                 : specName(W.backend);
      p = await api("/api/profiles", { method: "POST",
        body: { name: nm, backend: W.backend, values: {}, simple_visible: true,
                note: "初回セットアップで作成" } });
      W.cfgProfNew = true;
    }
    if (p) W.cfgProfId = p.id;
  } catch { /* 取得失敗は調整画面へスキップ */ }
  if (!cfg || !p) { renderCal(); go("s-cal"); return; }
  W.cfgSkip = false;
  W.calProfile = p;
  const v = p.values || {};
  $("#cf-sub").innerHTML = (W.cfgProfNew ? "新しい起動プロファイル <b>" : "プロファイル <b>")
    + esc(p.name) + "</b> に保存します。"
    + "キャリブレーションはこの設定で計測します。後からダッシュボードでも変更できます。";
  const nmEl = $("#cf-name");
  if (nmEl && document.activeElement !== nmEl) nmEl.value = p.name || "";
  const parts = [];
  // perUser 項目は保存値 (合計) を並列数で割って 1 人あたりとして見せる。
  // 並列数の初期値は保存値、無ければ「推奨」選択肢で揃える (フォーム描画前なので DOM を使わない)
  const parInit = (() => {
    if (!cfg.par) return 1;
    const s = parseInt(v[cfg.par.key] ?? "", 10);
    if (Number.isFinite(s) && s > 0) return s;
    const rec = cfg.par.opts.find(o => (o[2] || "").includes("推奨")) || cfg.par.opts[0] || ["1"];
    return parseInt(rec[0], 10) || 1;
  })();
  for (const id of ["ctx", "kv", "tp", "par", "vis"]) {
    const sec = cfg[id]; if (!sec) continue;
    const stored = String(v[sec.key] ?? "");
    const perNum = sec.perUser ? Math.round(Number(stored) / parInit) : NaN;
    const cur = (sec.perUser && stored && Number.isFinite(perNum)) ? String(perNum) : stored;
    // 未設定の項目は「推奨」(無ければ先頭) をプリチェックして、何も選まずに
    // 保存すると値が何も入らない状態を防ぐ
    const def = cur === "" ? ((sec.opts.find(o => (o[2] || "").includes("推奨"))
                               || sec.opts[0] || [""])[0]) : null;
    // 選択肢が 2つ以上の設定はプルダウンにまとめる (画面を縦に伸ばさない)。
    // 表示は数値と単位だけにし、data-t1 にサマリーへ使う短い名称を持たせる
    let rows = sec.opts.map(o => {
      const sel = cur ? cur === o[0] : o[0] === def;
      return `<option value="${esc(o[0])}"${sel ? " selected" : ""}`
        + ` data-t1="${esc(o[1])}">${esc(o[1])}`
        + (o[2] ? `（${esc(o[2])}）` : "") + "</option>";
    }).join("");
    if (cur && !sec.opts.some(o => o[0] === cur)) {
      // 選択肢に無い既存値 (手動設定や、合計でしか書かれない値) はそのまま維持。
      // data-raw は「JSON の値を据え置く」意味なので保存時に並列数を掛けない
      const t = esc(sec.cur ? sec.cur(sec.perUser ? cur : stored)
                            : (sec.perUser ? CF_TOK(cur) : stored));
      rows = `<option value="${esc(stored)}" selected data-t1="${t}"`
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
  cfSyncHints();   // 並列数を変えたら 1 人あたり / KV合計の補足も追従させる
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
  const cfg = RUNCFG[W.backend] || {};
  const err = $("#cf-err");
  err.classList.add("hidden");
  const vals = { ...(p.values || {}) };
  const on = p.on ? { ...p.on } : null;
  const chosen = [];
  const par = cfParNow();   // 一人あたり項目が合計を書き込むときに使う並列数 (フォームの選択)
  for (const id of ["ctx", "kv", "tp", "par", "vis"]) {
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
  if (cfg.model && !String((p.values || {})[modelKey] || "").trim()) {
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
    if (mp && W.backend === "basalt" && !mp.endsWith(".basalt")) {
      /* HF ダウンロードの result.path はスナップショット DIR。basalt の --model は
         .basalt ファイル正体なので、一覧の include (既定 pack) かキャッシュスキャンで
         ファイルへ解決する (解決できなければ DIR のまま = 起動時に正直なエラー) */
      const ent = (W.mdList || []).find(m => m.repo_id === String((W.job || {}).repo_id || ""));
      const want = (ent && ent.include && ent.include[0]) || "";
      let files = [];
      try {
        const hm = await api("/api/hf-models?refresh=1");   // 取得直後でキャッシュが古い可能性
        const snap = (hm.models || []).find(m => m.snapshot === mp);
        files = ((snap && snap.basalt) || []).map(f => f.name);
      } catch { /* スキャン失敗は include で */ }
      const fname = files.includes(want) ? want : (files[0] || want);
      if (fname) mp = mp.replace(/\/+$/, "") + "/" + fname;
    }
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
    if (cfg.regen) {
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
  $("#nt-sub").textContent = "プロファイル「" + p.name + "」に保存します。";
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
    const cfg = RUNCFG[W.backend] || {};
    if (cfg.regen) {
      await api(`/api/profiles/${p.id}/${cfg.regen === "config-json" ? "config-json" : "launch-json"}`,
                { method: "POST" });
    }
    await refreshProf(p); // 再生成後の config 込み最新を後続ステップへ
    W.summary.push({ ok: true, t: "接続設定を保存しました (" + chosen.join(" · ") + ")" });
    renderCal(); go("s-cal");
  } catch (e) {
    err.textContent = "保存に失敗しました: " + e.message;
    err.classList.remove("hidden");
  } finally { btn.disabled = false; }
});
$("#nt-skip").addEventListener("click", () => { renderCal(); go("s-cal"); });

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
    // 巻き込んで実行しないよう対象をこのバックエンドに限定する
    const calOff = !supported.includes(W.backend);
    const profs = ((st && st.profiles) || []).filter(p => supported.includes(p.backend)
      && p.backend === W.backend);
    ok = canCal && !calOff && profs.length > 0;
    W.calProfiles = profs;
    /* ⑤ の対象は ③ で作り/編集したプロファイル (W.calProfile)。re-load で
       W.calProfile を失ったときは「最近更新した自バックエンドのプロファイル」に
       倒す — 一覧先頭 (最古) を選んで他人のプロファイルを触る事故防止
       (2026-11-09: 旧プロファイル p_6acf49a3ad87 を掴んで config 整合チェックに弾かれた) */
    W.calPick = calPick(profs);
    if (calOff) {   // 調整が無いバックエンド (basalt 等) は調整画面を出さずに完了へ
      W.summary.push({ ok: true, t: "このバックエンドはキャリブレーション非対応のため、調整はスキップしました" });
      renderDone(); go("s-done"); return;
    }
    $("#ca-time").textContent = (profs.find(p => (p.backend || "").includes("freetoken"))
      ? "FreeToken は目安 3〜4 時間かかります" : "目安: 10〜30 分 (バックエンドにより変わります)");
    if (!canCal) $("#ca-sub").textContent = "調整はキャリブレーション権限のあるアカウントで実行できます。";
    else if (calOff) $("#ca-sub").textContent = "このバックエンドはキャリブレーションに対応していません (調整なしで完了できます)。";
    else if (!profs.length) $("#ca-sub").textContent = "調整対象のプロファイルがまだありません。プロファイルを作成してから、ダッシュボードの「キャリブレーション」で実行してください。";
    else $("#ca-sub").textContent = "調整 (キャリブレーション) はこの機械向けの性能調整です。実行中は GPU を独占します。"
      + "対象: " + (W.calPick ? W.calPick.name : "(未選択)");
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
  if (!run) { renderDone(); go("s-done"); return; }
  const profs = W.calProfiles || [];
  /* 対象は renderCal が決めた W.calPick (③ で作ったプロファイル優先)。
     profs[0] = 一覧の最古プロファイルで、Strata 側保存との config 整合チェックに
     弾かれるか別設定を調整してしまう (2026-11-09 の事故) */
  const p = W.calPick || calPick(profs) || profs[0];
  if (!p) { W.summary.push({ ok: false, t: "調整対象のプロファイルがなく、スキップしました" }); renderDone(); go("s-done"); return; }
  try {
    const job = await api("/api/calibrations", { method: "POST", body: { profile_id: p.id, restart_after: false } });
    W.kind = "cal"; W.job = job; W.logCursor = 0; W.logText = "";
    setProgChrome("ステップ 5 / 5 — 調整 (任意)", "この PC に調整しています…");
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
