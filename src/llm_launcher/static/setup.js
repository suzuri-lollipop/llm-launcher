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
                    precheck: "インストール内容を確認", verify: "動作確認 (import)" };
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
  cfgSkip: true, calProfile: null,
  cfgProfId: null, cfgProfNew: false, backends: null,
  summary: [],
};
const spec = id => (W.installs.installable || []).find(e => e.id === (id ?? W.backend)) || null;
const specName = id => (spec(id) || {}).name || id || "";
const toolLabel = () => W.installs.tool === "pip" ? "pip install" : "uv pip install";
const stepLabel = (name, kind) => kind === "install"
  ? (name === "install" ? toolLabel() : (INS_STEPS[name] || name))
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
               "s-cal": "s-net", "s-installed": "s-backend" };
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
    ["実行内容", `1. リポジトリを取得 (git clone)<br>2. 実行環境を作成 (.venv)<br>3. 依存パッケージを導入 (${esc(toolLabel())})`],
    ["この後の予定", doing],
  ];
  $("#rv-rows").innerHTML = rows.map(([k, v]) =>
    `<div class="row-item"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  $("#rv-preview").textContent = `git clone → .venv → ${toolLabel()} -e ".[${sp.extras || "accel"}]"`;
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
    ? (r.status === "done" ? "インストールが完了しました" : STEP_NAMES[r.status] && r.status !== "running" ? STEP_NAMES[r.status] : "インストール中…")
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
function finishJob() {
  const r = W.job || {};
  if (W.kind === "install") {
    if (r.status === "done") {
      W.summary.push({ ok: true, t: `${specName(r.backend)} をインストールしました` });
      $("#ok-title").textContent = `${specName(r.backend)} のインストールが完了しました`;
      $("#ok-sub").textContent = "次はモデルのダウンロードです。まだモデルが無い場合はダウンロードしてください。";
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

/* ---------------------------------------------------------------- 2: モデル */
function renderModel() {
  const sp = spec();
  const ms = (sp && sp.supported_models) || [];
  const sel = $("#md-sel");
  $("#md-rec-field").hidden = !ms.length;
  sel.innerHTML = '<option value="">その他 (下に直接入力)</option>' + ms.map(m => `
    <option value="${esc(m.repo_id)}">${esc(m.repo_id)}${m.name ? " — " + esc(m.name) : ""}</option>`).join("");
  const repo = $("#md-repo");
  if (ms.some(m => m.repo_id === repo.value.trim())) sel.value = repo.value.trim();
  sel.onchange = () => {
    if (sel.value) repo.value = sel.value;
    repo.focus();
    $("#md-start").disabled = !repo.value.trim();
  };
  const hasData = sp && sp.model_data;
  $("#md-dest").textContent = hasData
    ? `保存先: ${sp.name} のモデルフォルダ (起動設定に自動で反映されます)`
    : `保存先: HF キャッシュ (${W.installs.cacheDir || "既定"})`;
  $("#md-err").classList.add("hidden");
  $("#md-start").disabled = !repo.value.trim();
  $("#md-sub").textContent = ms.length
    ? "一覧から選ぶか、それ以外は repo id を直接入力してください。"
    : "Hugging Face の repo id を入力してください (例: Qwen/Qwen3-30B-A3B-Instruct)。";
}
$("#md-repo").addEventListener("input", () => { $("#md-start").disabled = !$("#md-repo").value.trim(); });
$("#md-start").addEventListener("click", async () => {
  const repo = $("#md-repo").value.trim();
  if (!repo) return;
  const sp = spec();
  const btn = $("#md-start");
  btn.disabled = true; btn.textContent = "開始中…";
  try {
    const job = await api("/api/installs/download", { method: "POST",
      body: { repo_id: repo, revision: "", backend: (sp && sp.model_data) ? sp.id : "", allow_patterns: [] } });
    W.kind = "download"; W.job = job;
    // 新しいセットアップ (モデル取得) を始めたので、稼働設定用の
    // プロファイルは作り直しになる
    W.cfgProfId = null; W.cfgProfNew = false;
    renderDownload(); go("s-download"); startPoll();
  } catch (e) {
    $("#md-err").textContent = "開始できませんでした: " + e.message;
    $("#md-err").classList.remove("hidden");
  } finally {
    btn.textContent = "ダウンロード"; if ($("#md-repo").value.trim()) btn.disabled = false;
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
  $("#dl-cancel").style.display = running ? "" : "none";
  $("#dl-retry").hidden = !bad;
  $("#dl-next").classList.toggle("hidden", r.status !== "done");
}
$("#dl-cancel").addEventListener("click", async () => {
  if (!W.job) return;
  if (!confirm("ダウンロードを中断しますか?")) return;
  try { await api(`/api/installs/${W.job.id}/cancel`, { method: "POST" }); }
  catch (e) { alert("中断できませんでした: " + e.message); }
});

/* ------------------------------------------------- 3b: 稼働設定 (調整の前) */
/* キャリブレーションは コンテキスト長 / KV形式 / 並列数 を前提に計測するため、
   調整の前にこの 3 値をプロファイルへ保存する。起動 JSON 主体のバックエンド
   (regen) は保存後に再生成しないと調整が古い argv で走る。 */
const CF_TOK = v => { const n = Math.round(Number(v));
  return (Number.isFinite(n) && n % 1024 === 0 && n >= 1024)
    ? `${Math.round(n / 1024)}K トークン` : `${Number(v).toLocaleString()} トークン`; };
const CF_CTX = [
  ["32768", "32K トークン", "軽快・省メモリ。短いやり取り中心向け", ""],
  ["131072", "128K トークン", "長めの文書も扱える実用バランス", "推奨"],
  ["262144", "256K トークン", "最大構成。VRAM を多く消費します", ""],
];
const CF_KV = [
  ["fp8", "fp8", "KVキャッシュを約半分のメモリに。品質はほぼ同等", "推奨"],
  ["bf16", "bf16 (fp16系)", "品質最優先。KVメモリは約2倍", ""],
  ["auto", "auto (エンジン既定値)", "エンジンにおまかせ", ""],
];
const CF_PAR = [
  ["1", "1 並列", "1リクエストずつ処理。最も安定", ""],
  ["2", "2 並列", "少人数の同時利用向け", ""],
  ["4", "4 並列", "数人の同時利用の目安", "推奨"],
  ["8", "8 並列", "大規模並列。高VRAM必須", ""],
];
const CF_TP = [
  ["1", "1 GPU", "分割なし。1枚のGPUにモデル全体を載せる", ""],
  ["2", "2 GPU", "モデルを2枚に分割。GPUあたりのメモリが下がる", ""],
  ["4", "4 GPU", "4枚に分割。大きなモデル向け", ""],
  ["8", "8 GPU", "8枚に分割。8枚ある環境用", ""],
];
const RUNCFG = {
  "freetoken-fork": { regen: true, model: true,
    ctx: { key: "num_tokens", label: "コンテキスト長",
           hint: "長いほどVRAMを消費します。調整はこの長さ前提のKVキャッシュで行います", opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式 (fp8 / bf16 など)",
          hint: "会話履歴の格納精度です。fp8はメモリ半減、bf16は品質優先", opts: CF_KV, cur: v => v },
    tp: { key: "tp_size", label: "Tensor Parallel (GPU分割数)",
          hint: "モデルを何枚のGPUに分割するか。VRAMが足りない場合は増やします", opts: CF_TP, cur: v => `${v} GPU` },
    par: { key: "max_running_requests", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: v => `${v} 並列` } },
  "sglang": { regen: false,
    ctx: { key: "context_length", label: "コンテキスト長",
           hint: "長いほどVRAMを消費します", opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式",
          hint: "fp8はメモリ半減、bf16は品質優先", opts: CF_KV, cur: v => v },
    par: { key: "max_running_requests", label: "並列推論数",
           hint: "同時に処理するリクエスト数", opts: CF_PAR, cur: v => `${v} 並列` } },
  "vllm": { regen: false,
    ctx: { key: "max_model_len", label: "コンテキスト長",
           hint: "長いほどVRAMを消費します", opts: CF_CTX, cur: CF_TOK },
    kv: { key: "kv_cache_dtype", label: "KVキャッシュ形式",
          hint: "fp8はメモリ半減、bf16は品質優先", opts: CF_KV, cur: v => v },
    par: { key: "max_num_seqs", label: "並列推論数",
           hint: "同時に処理するリクエスト数", opts: CF_PAR, cur: v => `${v} 並列` } },
  "strata": { regen: false,
    par: { key: "parallel", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: v => `${v} 並列` } },
  "maya": { regen: false,
    par: { key: "parallel", label: "並列推論数",
           hint: "同時に処理するリクエスト数。調整はこの本数で実測します", opts: CF_PAR, cur: v => `${v} 並列` } },
};
async function renderConfig() {
  W.cfgSkip = true;
  const cfg = RUNCFG[W.backend];
  let p = null;
  try {
    const j = W.job;
    // このセットアップでモデルを取得した = 「新規インストール」の進入
    const fresh = !!(j && j.kind === "download" && j.status === "done"
                     && j.backend === W.backend);
    const profs = await api("/api/profiles");
    if (W.cfgProfId) {
      // 戻る/次への往復では、このセットアップで作ったプロファイルを再利用
      p = (profs || []).find(x => x.id === W.cfgProfId) || null;
    } else if (!p && cfg && (fresh || !(profs || []).some(x => x.backend === W.backend))) {
      // セットアップ時のプロファイルは必ず新規作成する
      // (既存プロファイルへ書き込むと、そのプロファイルのモデルが
      //  置き換わってしまうため)。起動 JSON を指定した形になるのは保存時。
      const nm = (fresh && j.repo_id) ? String(j.repo_id).split("/").pop()
                                      : specName(W.backend);
      p = await api("/api/profiles", { method: "POST",
        body: { name: nm, backend: W.backend, values: {}, simple_visible: true } });
      W.cfgProfNew = true;
    } else {
      p = (profs || []).find(x => x.backend === W.backend) || null;
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
  for (const id of ["ctx", "kv", "tp", "par"]) {
    const sec = cfg[id]; if (!sec) continue;
    const cur = String(v[sec.key] ?? "");
    // 未設定の項目は「推奨」(無ければ先頭) をプリチェックして、何も選まずに
    // 保存すると値が何も入らない状態を防ぐ
    const def = cur === "" ? ((sec.opts.find(o => (o[3] || "").includes("推奨"))
                               || sec.opts[0] || [""])[0]) : null;
    let rows = sec.opts.map(o => {
      const sel = cur ? cur === o[0] : o[0] === def;
      return `<label class="opt${sel ? " sel" : ""}"><input type="radio" name="cf-${id}" value="${esc(o[0])}"${sel ? " checked" : ""}>`
        + `<span class="txt"><span class="t1">${esc(o[1])}</span>`
        + (o[2] ? `<span class="t2">${esc(o[2])}</span>` : "") + "</span>"
        + (o[3] ? `<span class="tag">${esc(o[3])}</span>` : "") + "</label>";
    }).join("");
    if (cur && !sec.opts.some(o => o[0] === cur)) {
      rows = `<label class="opt sel"><input type="radio" name="cf-${id}" value="${esc(cur)}" checked>`
        + `<span class="txt"><span class="t1">${esc(sec.cur ? sec.cur(cur) : cur)}</span>`
        + `<span class="t2">現在の値 (そのまま維持)</span></span></label>` + rows;
    }
    parts.push(`<div class="cf-group"><div class="cf-label">${esc(sec.label)}</div>`
      + `<div class="cf-hint">${esc(sec.hint)}</div><div class="opt-list">${rows}</div></div>`);
  }
  $("#cf-groups").innerHTML = parts.join("");
  $("#cf-err").classList.add("hidden");
}
$("#cf-groups").addEventListener("change", e => {
  const inp = e.target.closest("input"); if (!inp || inp.type !== "radio") return;
  document.querySelectorAll(`#cf-groups input[name="${inp.name}"]`).forEach(i =>
    i.closest(".opt").classList.toggle("sel", i.checked));
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
  for (const id of ["ctx", "kv", "tp", "par"]) {
    const sec = cfg[id]; if (!sec) continue;
    const el = document.querySelector(`#cf-groups input[name="cf-${id}"]:checked`);
    if (!el) continue;
    vals[sec.key] = el.value;
    if (on) on[sec.key] = true;
    const t1 = el.closest("label").querySelector(".t1");
    chosen.push(sec.label.split(" ")[0] + " " + (t1 ? t1.textContent.trim() : el.value));
  }
  // モデル未決定のプロファイルは今回ダウンロードしたモデルを指す。ページ
  // リロードで W.job を失ってもインストール履歴 (jobs) から復元する。新規
  // プロファイルでモデルなしを保存すると --model-path 無しで起動不能になる
  // ため、解決できなければ保存せず前に戻す (2026-10-30 の実害の再発防止)。
  if (cfg.model && !String((p.values || {}).model_path || "").trim()) {
    let mp = "";
    const j = W.job;
    if (j && j.kind === "download" && j.status === "done" && j.backend === W.backend)
      mp = String((j.result && j.result.path) || j.local_dir || "");
    if (!mp) {
      try {
        const ins = await api("/api/installs");
        const done = ((ins && ins.jobs) || []).filter(x => x.kind === "download"
          && x.status === "done" && x.backend === W.backend);
        const dj = done[done.length - 1];
        if (dj) mp = String((dj.result && dj.result.path) || dj.local_dir || "");
      } catch { /* 履歴が取れなければ空のまま */ }
    }
    if (mp) {
      vals.model_path = mp;
      if (on) on.model_path = true;
      chosen.push("モデル " + mp.split("/").pop());
    } else if (W.cfgProfNew) {
      err.textContent = "モデルがまだ決まっていません。前に戻ってモデルをダウンロードしてください";
      err.classList.remove("hidden");
      return;
    }
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
      // 調整と起動は起動 JSON を正として読むため、保存後は必ず再生成する
      await api(`/api/profiles/${p.id}/launch-json`, { method: "POST" });
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
      await api(`/api/profiles/${p.id}/launch-json`, { method: "POST" });
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
async function renderCal() {
  const runOpt = $$("#ca-list input").find(i => i.value === "run");
  const canCal = W.me && (W.me.role === "admin" || !W.me.auth_enabled || (W.me.perms || {}).calibrate);
  let ok = false;
  try {
    const [c, st] = await Promise.all([api("/api/calibrations").catch(() => null),
                                       api("/api/state").catch(() => null)]);
    const supported = (c && c.supported) || [];
    const profs = ((st && st.profiles) || []).filter(p => supported.includes(p.backend));
    ok = canCal && profs.length > 0;
    W.calProfiles = profs;
    $("#ca-time").textContent = (profs.find(p => (p.backend || "").includes("freetoken"))
      ? "FreeToken は目安 3〜4 時間かかります" : "目安: 10〜30 分 (バックエンドにより変わります)");
    if (!canCal) $("#ca-sub").textContent = "調整はキャリブレーション権限のあるアカウントで実行できます。";
    else if (!profs.length) $("#ca-sub").textContent = "調整対象のプロファイルがまだありません。プロファイルを作成してから、ダッシュボードの「キャリブレーション」で実行してください。";
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
  const sp = spec();
  const profs = W.calProfiles || [];
  const p = profs.find(x => x.backend === W.backend) || profs.find(x => (sp || {}).name && x.backend === (sp.id)) || profs[0];
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
