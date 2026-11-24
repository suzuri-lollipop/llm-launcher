/* ===================================================================
   view: coding — コーディングベンチマーク (pi ハーネスで実装を作って比較)
   ベンチマークタブ内の coding モードとして差し込まれる (旧独立タブは統合済み)
   =================================================================== */
"use strict";
VIEWS.coding = `
  <div id="view-coding">
    <h2 class="view-title">コーディングベンチ <span class="muted">pi ハーネスにプロファイルのサーバーでバックエンド / フロントエンドを作らせ、実装を並べて比較</span></h2>
    <div class="bm-tabs" role="tablist">
      <button class="bm-tab" id="cbt-run" role="tab" aria-selected="false" onclick="cbSetTab('run')"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 実行<span class="run-dot hidden" id="cbt-run-dot" title="コーディングベンチ実行中"></span></button>
      <button class="bm-tab" id="cbt-history" role="tab" aria-selected="false" onclick="cbSetTab('history')"><svg class=ic aria-hidden=true><use href=#i-history /></svg> 履歴 <span class="n" id="cbt-hn">0</span></button>
      <button class="bm-tab" id="cbt-viewer" role="tab" aria-selected="false" onclick="cbSetTab('viewer')"><svg class=ic aria-hidden=true><use href=#i-layers /></svg> ビューアー</button>
      <button class="bm-tab" id="cbt-eval" role="tab" aria-selected="false" onclick="cbSetTab('eval')"><svg class=ic aria-hidden=true><use href=#i-chart /></svg> AI評価</button>
    </div>
    <div id="cbt-p-run">
    <div id="cb-active"></div>
    <div class="card">
      <div class="row" style="gap:8px;align-items:center">
        <label class="f" for="cb-target" style="margin:0">対象</label>
        <select id="cb-target" style="min-width:220px" onchange="cbTargetChange()">
          <option value="profile">起動プロファイル (ローカル)</option>
          <option value="external">外部 API (OpenAI 互換)</option>
        </select>
        <span class="muted" id="cb-target-note" style="font-size:11px"></span>
      </div>
      <div id="cb-target-profile">
      <div class="row" style="gap:8px;margin-top:8px">
        <select id="cb-profile" class="grow" style="min-width:180px" onchange="cbPersist();cbLoadModelOptions()"></select>
        <span class="badge" id="cb-prof-state"><span class="dot"></span>-</span>
      </div>
      </div>
      <div id="cb-target-external" class="hidden">
        <div style="margin-top:8px">
          <label class="f" for="cb-ext-base">Base URL <span class="muted">(OpenAI 互換。例: https://api.openai.com/v1 — /v1 は省略可)</span></label>
          <input type="text" id="cb-ext-base" class="mono" placeholder="https://api.openai.com/v1" style="width:100%">
        </div>
        <div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap">
          <div class="grow" style="min-width:200px"><label class="f" for="cb-ext-key">API キー <span class="muted">(履歴にはマスク保存・実行後は破棄)</span></label>
            <input type="password" id="cb-ext-key" class="mono" autocomplete="off" placeholder="sk-..." style="width:100%"></div>
          <div class="grow" style="min-width:180px"><label class="f" for="cb-ext-model">モデル名 <span class="muted">(空 = 一覧の先頭)</span></label>
            <input type="text" id="cb-ext-model" class="mono" placeholder="gpt-5.2" style="width:100%"></div>
        </div>
        <div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap">
          <div class="grow" style="min-width:160px"><label class="f" for="cb-ext-name">表示ラベル <span class="muted">(空 = 自動)</span></label>
            <input type="text" id="cb-ext-name" maxlength="120" placeholder="例: GPT-5.2 クラウド" style="width:100%"></div>
          <div><label class="f" for="cb-ext-ctx">コンテキスト長</label>
            <input type="number" id="cb-ext-ctx" min="1024" max="2000000" value="128000" style="width:110px"></div>
          <button class="ghost" onclick="cbExternalCheck()" style="align-self:flex-end;min-height:36px">${IC("refresh")} 接続テスト</button>
          <span class="badge" id="cb-ext-check" style="align-self:flex-end"><span class="dot"></span>未確認</span>
        </div>
      </div>
      <div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap">
        <span class="badge" id="cb-pi-state"><span class="dot"></span>pi 確認中…</span>
        <button class="ghost need-bench hidden" id="cb-pi-install" onclick="cbInstallPi()" style="min-height:32px;padding:4px 10px"><svg class=ic aria-hidden=true><use href=#i-download /></svg> pi をインストール</button>
        <span class="badge" id="cb-sandbox-state"><span class="dot"></span>サンドボックス確認中…</span>
        <span class="muted" id="cb-env-note" style="font-size:11px"></span>
      </div>
      <div class="muted" style="margin-top:6px">ハーネス: <a href="https://github.com/earendil-works/pi" target="_blank" rel="noopener">pi (earendil-works/pi)</a> をサンドボックス (bubblewrap / Docker 自動選択) 内で実行します。ファイルアクセスは実行ディレクトリとスキルフォルダに制限されます。</div>
      <div style="margin-top:10px">
        <label class="f" for="cb-skills">スキル群フォルダ <span class="muted">(SKILL.md を再帰的に読み込み)</span></label>
        <div class="row" style="gap:6px">
          <input type="text" id="cb-skills" class="mono grow" placeholder="(デフォルト)" style="min-width:200px">
          <button class="ghost" onclick="cbResetField('skills')" style="min-height:36px">既定に戻す</button>
        </div>
        <div class="muted" id="cb-skills-note" style="font-size:11px;margin-top:2px"></div>
      </div>
      <div style="margin-top:8px">
        <label class="f" for="cb-agents">AGENTS.md <span class="muted">(ワークスペースにコピーされ pi が読み込む)</span></label>
        <div class="row" style="gap:6px">
          <input type="text" id="cb-agents" class="mono grow" placeholder="(デフォルト)" style="min-width:200px">
          <button class="ghost" onclick="cbResetField('agents')" style="min-height:36px">既定に戻す</button>
        </div>
      </div>
      <div style="margin-top:8px">
        <label class="f" for="cb-backend">バックエンド作成プロンプト <span class="muted">(フェーズ 1)</span></label>
        <textarea id="cb-backend" class="cb-prompt mono" rows="7" placeholder="(デフォルト)"></textarea>
        <div class="row" style="gap:6px;margin-top:2px"><button class="ghost" onclick="cbResetField('backend')" style="min-height:32px;padding:4px 10px">既定に戻す</button><span class="muted" id="cb-backend-n" style="font-size:11px"></span></div>
      </div>
      <div style="margin-top:8px">
        <label class="f" for="cb-frontend">フロントエンド作成プロンプト <span class="muted">(フェーズ 2・同じセッションを継続)</span></label>
        <textarea id="cb-frontend" class="cb-prompt mono" rows="7" placeholder="(デフォルト)"></textarea>
        <div class="row" style="gap:6px;margin-top:2px"><button class="ghost" onclick="cbResetField('frontend')" style="min-height:32px;padding:4px 10px">既定に戻す</button><span class="muted" id="cb-frontend-n" style="font-size:11px"></span></div>
      </div>
      <div class="row" style="gap:12px;flex-wrap:wrap;margin-top:10px">
        <div id="cb-model-wrap"><label class="f" for="cb-model">モデル名 <span class="muted">(稼働中サーバーから自動取得)</span></label>
          <div class="row" style="gap:4px">
            <select id="cb-model" class="mono" style="width:230px"><option value="">(開始時に自動解決)</option></select>
            <button class="ghost" onclick="cbLoadModelOptions(true)" title="モデル一覧を再取得" style="min-height:36px;padding:4px 8px">${IC("refresh")}</button>
          </div>
        </div>
        <div><label class="f" for="cb-thinking">thinking <span class="muted">(モデルで設定できるもののみ)</span></label>
          <select id="cb-thinking" style="width:110px"><option value="off">off</option></select>
          <div class="muted" id="cb-think-note" style="font-size:10.5px;max-width:220px"></div>
        </div>
        <div><label class="f" for="cb-ptimeout">1 フェーズ上限 (秒)</label><input type="number" id="cb-ptimeout" min="30" max="14400" value="1800" style="width:100px"></div>
        <div class="grow" style="min-width:160px"><label class="f" for="cb-name">ラベル <span class="muted">(空 = 自動)</span></label><input type="text" id="cb-name" maxlength="120" placeholder="例: Qwen3-30B タスク管理" style="width:100%"></div>
      </div>
      <div class="row" style="gap:18px;flex-wrap:wrap;margin-top:8px">
        <span id="cb-local-opts" style="display:contents">
        <label class="switch"><input type="checkbox" id="cb-autostart" checked><span class="track"></span><span class="lab" style="font-size:12.5px">未起動ならサーバーを自動起動</span></label>
        <label class="switch"><input type="checkbox" id="cb-stopafter"><span class="track"></span><span class="lab" style="font-size:12.5px">完了後、自動起動したサーバーを停止</span></label>
        </span>
        <label class="switch"><input type="checkbox" id="cb-sandbox" checked><span class="track"></span><span class="lab" style="font-size:12.5px">サンドボックス内で実行</span></label>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="primary need-bench" id="cb-start" onclick="cbStart()"><svg class=ic aria-hidden=true><use href=#i-play /></svg> コーディングベンチ開始</button>
        <span class="grow"></span>
        <span class="muted" id="cb-note"></span>
      </div>
    </div>
    </div>
    <div id="cbt-p-history" class="hidden"><div id="cb-history"></div></div>
    <div id="cbt-p-viewer" class="hidden"><div id="cb-viewer"></div></div>
    <div id="cbt-p-eval" class="hidden"><div id="cb-eval"></div></div>
  </div>
`;
/* ============================================================= coding bench */
/* pi ハーネス (https://github.com/earendil-works/pi) にプロファイルのサーバーで
   バックエンド → フロントエンドの順に実装を作らせる。実行はバックエンド
   (POST /api/coding/runs) がサンドボックス内で走らせ、2.5 秒ポーリングで進捗を描画。
   ビューアーは履歴から 2 件選んでプレビュー (生成アプリの起動 + 中継) を並べる。 */
const CB_KEY = "llm-laun…h.v1";
const CB_TABS = ["run", "history", "viewer", "eval"];
const CB_INPUTS = ["cb-profile", "cb-skills", "cb-agents", "cb-backend", "cb-frontend",
                   "cb-model", "cb-thinking", "cb-ptimeout", "cb-autostart",
                   "cb-stopafter", "cb-sandbox", "cb-name",
                   "cb-target", "cb-ext-base", "cb-ext-model", "cb-ext-name", "cb-ext-ctx"];
/* 外部 API の thinking 段階 (OpenAI 標準の reasoning_effort 配線。サーバー側
   テンプレートが未知なので chat_template_kwargs は送らない。xhigh/max は
   対応プロバイダのみ通るため UI は標準 5 段階 + off まで) */
const CB_EXT_LEVELS = ["off", "minimal", "low", "medium", "high"];
const CB_PHASES = {queued: "待機", launching: "サーバー起動", waiting: "health 待ち",
                   backend: "バックエンド作成", frontend: "フロントエンド作成",
                   scanning: "走査", done: "完了"};

function cbInit() {
  let d = {};
  try { d = JSON.parse(localStorage.getItem(CB_KEY) || "{}") || {}; } catch { d = {}; }
  S.coding = { runs: [], active: null, assets: null, status: null, busy: false,
               tab: CB_TABS.includes(d.tab) ? d.tab : "run",
               selA: d.selA || "", selB: d.selB || "",
               cmp: Array.isArray(d.cmp) ? d.cmp : [],
               modeA: "preview", modeB: "preview",
               filesA: null, filesB: null, codeA: null, codeB: null,
               logOpen: false, logPhase: "backend", logText: "",
               tx: {rid: "", phase: "backend", data: null, loading: false, error: ""},
               _act: "", _hist: "", _eval: "", _psigA: "", _psigB: "", _codeRev: 0, _fv: {},
               evals: [], evalBusy: false, evalOpen: "",
               judgeKind: d.judgeKind || "profile", judgePid: d.judgePid || "",
               judgeExtBase: d.judgeExtBase || "", judgeExtModel: d.judgeExtModel || "",
               judgeThinking: d.judgeThinking || "off" };
  const set = (id, v) => { const el = $(id); if (el && v != null && v !== "") {
    if (el.type === "checkbox") el.checked = !!v; else el.value = v; } };
  set("#cb-profile", d.profile); set("#cb-skills", d.skills); set("#cb-agents", d.agents);
  set("#cb-backend", d.backend); set("#cb-frontend", d.frontend);
  set("#cb-model", d.model); set("#cb-thinking", d.thinking);
  set("#cb-ptimeout", d.ptimeout);
  set("#cb-name", d.name);
  set("#cb-target", d.target); set("#cb-ext-base", d.extBase);
  set("#cb-ext-model", d.extModel); set("#cb-ext-name", d.extName); set("#cb-ext-ctx", d.extCtx);
  // 選択肢は非同期ロード前に復元値を覚えておく (cbLoadModelOptions が適用)。
  // effort 段階 (minimal/low/.../max) もそのまま保持し、語彙に無ければ先頭に落とす
  S.coding._restored = {profile: d.profile || "", model: d.model || "",
                        thinking: d.thinking || "off"};
  if (d.autostart === false) $("#cb-autostart").checked = false;
  if (d.stopafter === true) $("#cb-stopafter").checked = true;
  if (d.sandbox === false) $("#cb-sandbox").checked = false;
  CB_INPUTS.forEach(id => { const el = $(`#${id}`); if (el) el.addEventListener("change", cbPersist); });
  ["cb-backend", "cb-frontend"].forEach(id => {
    const el = $(`#${id}`); if (el) el.addEventListener("input", cbPromptCount);
  });
  cbPromptCount();
  cbLoadAssets();
  cbTargetChange();   // 対象 (プロファイル / 外部 API) の表示切替 + thinking 選択肢
}
function cbPersist() {
  if (!S.coding) return;
  const v = id => $(id) ? $(id).value : "";
  try {
    localStorage.setItem(CB_KEY, JSON.stringify({
      profile: v("#cb-profile"), skills: v("#cb-skills"), agents: v("#cb-agents"),
      backend: v("#cb-backend"), frontend: v("#cb-frontend"),
      model: v("#cb-model"), thinking: v("#cb-thinking"), ptimeout: v("#cb-ptimeout"),
      name: v("#cb-name"),
      autostart: $("#cb-autostart").checked, stopafter: $("#cb-stopafter").checked,
      sandbox: $("#cb-sandbox").checked,
      tab: S.coding.tab, selA: S.coding.selA, selB: S.coding.selB,
      cmp: S.coding.cmp,
      target: v("#cb-target"), extBase: v("#cb-ext-base"), extModel: v("#cb-ext-model"),
      extName: v("#cb-ext-name"), extCtx: v("#cb-ext-ctx"),   // API キーは保存しない
      judgeKind: S.coding.judgeKind, judgePid: S.coding.judgePid,
      judgeExtBase: S.coding.judgeExtBase, judgeExtModel: S.coding.judgeExtModel,
      judgeThinking: S.coding.judgeThinking,
    }));
  } catch { /* ignore */ }
}
/* 対象切替 (起動プロファイル / 外部 API): フォームと thinking 選択肢を差し替える */
function cbTargetChange() {
  const ext = $("#cb-target").value === "external";
  $("#cb-target-profile").classList.toggle("hidden", ext);
  $("#cb-target-external").classList.toggle("hidden", !ext);
  $("#cb-model-wrap").classList.toggle("hidden", ext);
  $("#cb-local-opts").style.display = ext ? "none" : "contents";
  $("#cb-target-note").textContent = ext
    ? "クラウド等の OpenAI 互換 API で実行します (GPU 実走ロック不要・ローカルベンチと共存可)" : "";
  const tsel = $("#cb-thinking");
  if (ext) {
    const want = tsel.value;
    tsel.innerHTML = CB_EXT_LEVELS.map(l => `<option value="${l}">${l}</option>`).join("");
    tsel.value = CB_EXT_LEVELS.includes(want) ? want : "off";
    tsel.disabled = false;
    $("#cb-think-note").textContent = "reasoning_effort を送信 (プロバイダが対応する段階のみ有効)";
  } else {
    cbLoadModelOptions();
  }
  cbPersist();
}
async function cbExternalCheck() {
  const base = $("#cb-ext-base").value.trim();
  if (!base) { toast("Base URL を入力してください"); return; }
  const badge = $("#cb-ext-check");
  badge.className = "badge warn"; badge.innerHTML = '<span class="dot"></span>確認中…';
  try {
    const r = await api("/api/coding/external-check", {method: "POST", body: {
      base_url: base, api_key: $("#cb-ext-key").value, model: $("#cb-ext-model").value.trim(),
    }});
    if (r.ok) {
      badge.className = "badge ok";
      badge.innerHTML = `<span class="dot"></span>OK (${r.models.length} モデル)`;
      badge.title = r.models.slice(0, 30).join("\n");
      toast(r.warning ? `接続 OK。${r.warning}` : `接続 OK: モデル ${r.models.length} 件`);
    } else {
      badge.className = "badge err"; badge.innerHTML = '<span class="dot"></span>失敗';
      badge.title = r.error || ""; toast("接続失敗: " + (r.error || "不明"));
    }
  } catch (e) {
    badge.className = "badge err"; badge.innerHTML = '<span class="dot"></span>失敗';
    toast("接続テスト失敗: " + e.message);
  }
}
function cbPromptCount() {
  const b = $("#cb-backend"), f = $("#cb-frontend");
  if (b && $("#cb-backend-n")) $("#cb-backend-n").textContent = `${b.value.length} 文字`;
  if (f && $("#cb-frontend-n")) $("#cb-frontend-n").textContent = `${f.value.length} 文字`;
}
async function cbLoadAssets() {
  try {
    S.coding.assets = await api("/api/coding/assets");
    S.coding.status = await api("/api/coding/status");
  } catch { return; }
  const a = S.coding.assets;
  // 未入力の資材欄だけ既定値で埋める (ユーザーの編集は上書きしない)
  const fill = (id, v) => { const el = $(id); if (el && !el.value) el.value = v; };
  fill("#cb-skills", a.skills_dir); fill("#cb-agents", a.agents_path);
  fill("#cb-backend", a.backend_prompt); fill("#cb-frontend", a.frontend_prompt);
  cbPromptCount(); cbPersist();
  if (codingVisible()) renderCoding();
}
function cbResetField(kind) {
  const a = S.coding && S.coding.assets;
  if (!a) { toast("デフォルト資材を取得中です"); return; }
  const map = {skills: ["#cb-skills", a.skills_dir], agents: ["#cb-agents", a.agents_path],
               backend: ["#cb-backend", a.backend_prompt], frontend: ["#cb-frontend", a.frontend_prompt]};
  const [id, v] = map[kind];
  $(id).value = v;
  cbPromptCount(); cbPersist();
}
/* モデル名自動取得 + thinking 選択肢 (モデルで設定できるものだけ) */
async function cbLoadModelOptions(refresh) {
  const d = S.coding;
  if ($("#cb-target") && $("#cb-target").value === "external") return;   // 外部 API は固定段階
  const pid = $("#cb-profile").value;
  d._optsPid = pid;
  const msel = $("#cb-model"), tsel = $("#cb-thinking");
  if (!pid) {
    msel.innerHTML = `<option value="">(開始時に自動解決)</option>`;
    tsel.innerHTML = `<option value="off">off</option>`;
    $("#cb-think-note").textContent = "";
    return;
  }
  const wantModel = msel.value || (d._restored && d._restored.model) || "";
  let wantThink = tsel.value || (d._restored && d._restored.thinking) || "off";
  let o;
  try { o = await api(`/api/coding/model-options?profile_id=${encodeURIComponent(pid)}`); }
  catch (e) { if (refresh) toast("モデル情報取得失敗: " + e.message); return; }
  d.modelOpts = o;
  // モデル一覧: 取得できたら先頭を自動選択。未起動なら「開始時に自動解決」
  if (o.models && o.models.length) {
    msel.innerHTML = o.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    msel.value = o.models.includes(wantModel) ? wantModel : o.models[0];
  } else {
    msel.innerHTML = `<option value="">(開始時に自動解決)</option>`;
  }
  // thinking: 非思考モデルは off のみ / 思考モデルは off+on / effort 語彙があれば off+段階
  const levels = (o.thinking && o.thinking.levels) || ["off"];
  // 旧保存値 "on"/"high" は high があれば high、無ければサーバー既定段階へ読み替える
  // (Qwen3.8 の語彙は xhigh/medium/low で high が無く、放置すると off に落ちる)
  if (["on", "high"].includes(wantThink) && !levels.includes(wantThink) && levels.length > 1) {
    const cap0 = o.thinking || {};
    wantThink = levels.includes("high") ? "high"
      : (cap0.default && levels.includes(cap0.default)) ? cap0.default : levels[1];
  }
  tsel.innerHTML = levels.map(l => `<option value="${l}">${l}</option>`).join("");
  tsel.value = levels.includes(wantThink) ? wantThink : levels[0];
  tsel.disabled = levels.length <= 1;
  const cap = o.thinking || {};
  $("#cb-think-note").textContent = cap.capable
    ? `根拠: ${cap.source}${cap.qwen ? " (enable_thinking 配線)" : ""}`
    : "このモデルは thinking 非対応 (off のみ)";
  if (refresh) toast(o.models && o.models.length
    ? `モデル ${o.models.length} 件を取得しました` : "サーバー未起動のためモデル名は開始時に解決します");
  cbPersist();
}
function cbSetTab(t) {
  if (!CB_TABS.includes(t)) return;
  S.coding.tab = t; cbPersist(); renderCoding();
  if (t === "eval") {
    api("/api/coding/evals").then(r => {
      S.coding.evals = r.evals || []; renderCodingEval();
    }).catch(() => {});
  }
}
async function cbPoll(force) {
  if (!S.coding) return;
  try {
    const d = await api("/api/coding/runs");
    S.coding.runs = d.runs || []; S.coding.active = d.active || null;
    if (codingVisible()) S.coding.status = await api("/api/coding/status");
    if (codingVisible() && (S.coding.tab === "eval" || S.coding.evals.some(e => e.status === "running"))) {
      const ev = await api("/api/coding/evals");
      S.coding.evals = ev.evals || [];
    }
  } catch { return; }
  bmBadge();   // 統合タブのバッジ (速度ベンチ / コーディングベンチ共通)
  try { if (codingVisible() || force) renderCoding(); }
  catch (e) { console.error("coding render error:", e); }
  // 開いている出力履歴が実行中の実行なら自動更新 (ライブ閲覧)
  const tx = S.coding.tx;
  if (tx.rid && !tx.loading &&
      S.coding.runs.some(r => r.id === tx.rid && ["queued", "running"].includes(r.status))) {
    cbFetchTranscript();
  }
}
async function cbInstallPi() {
  try {
    await api("/api/coding/pi/install", {method: "POST"});
    toast("pi のインストールを開始しました (npm install -g)");
  } catch (e) { toast("インストール開始失敗: " + e.message); }
  cbPoll(true);
}
async function cbStart() {
  const d = S.coding;
  const ext = $("#cb-target").value === "external";
  if (ext) {
    if (!$("#cb-ext-base").value.trim()) { toast("外部 API の Base URL を入力してください"); return; }
  } else if (!$("#cb-profile").value) { toast("プロファイルを選択してください"); return; }
  d.busy = true; renderCoding(); cbPersist();
  try {
    const body = {
      name: $("#cb-name").value,
      skills_dir: $("#cb-skills").value,
      agents_path: $("#cb-agents").value,
      backend_prompt: $("#cb-backend").value,
      frontend_prompt: $("#cb-frontend").value,
      thinking: $("#cb-thinking").value === "on" ? "high" : $("#cb-thinking").value,
      phase_timeout: +$("#cb-ptimeout").value || 1800,
      sandbox: $("#cb-sandbox").checked,
    };
    if (ext) {
      body.external = {
        base_url: $("#cb-ext-base").value.trim(),
        api_key: $("#cb-ext-key").value,
        model: $("#cb-ext-model").value.trim(),
        name: $("#cb-ext-name").value.trim(),
        context_window: +$("#cb-ext-ctx").value || 128000,
      };
    } else {
      body.profile_id = $("#cb-profile").value;
      body.model = $("#cb-model").value;
      body.auto_start = $("#cb-autostart").checked;
      body.stop_after = $("#cb-stopafter").checked;
    }
    const run = await api("/api/coding/runs", {method: "POST", body});
    toast(`コーディングベンチを開始しました: ${run.name}`);
    d.logOpen = false;
    await cbPoll(true);
  } catch (e) { toast("開始失敗: " + e.message); }
  finally { d.busy = false; renderCoding(); }
}
async function cbCancel(id) {
  try { await api(`/api/coding/runs/${id}/cancel`, {method: "POST"}); toast("中断を要求しました"); }
  catch (e) { toast("中断失敗: " + e.message); }
  cbPoll(true);
}
async function cbDelete(id) {
  if (!confirm("この実行履歴と生成ファイルを削除しますか?")) return;
  try { await api(`/api/coding/runs/${id}`, {method: "DELETE"}); toast("削除しました"); }
  catch (e) { toast("削除失敗: " + e.message); }
  if (S.coding.selA === id) S.coding.selA = "";
  if (S.coding.selB === id) S.coding.selB = "";
  if (S.coding.tx.rid === id) S.coding.tx = {rid: "", phase: S.coding.tx.phase, data: null, loading: false, error: ""};
  cbPoll(true);
}
async function cbClearAll() {
  if (!confirm("完了済みのコーディングベンチ履歴をすべて削除しますか? (生成ファイルも削除されます)")) return;
  try { const r = await api("/api/coding/runs", {method: "DELETE"}); toast(`${r.removed} 件削除しました`); }
  catch (e) { toast("削除失敗: " + e.message); }
  S.coding.tx = {rid: "", phase: S.coding.tx.phase, data: null, loading: false, error: ""};
  cbPoll(true);
}
function cbRerun(id) {
  const r = S.coding.runs.find(x => x.id === id);
  if (!r) return;
  const p = r.params || {};
  const set = (fid, v) => { const el = $(fid); if (el && v != null) el.value = v; };
  set("#cb-skills", p.skills_dir); set("#cb-agents", p.agents_path);
  set("#cb-backend", p.backend_prompt); set("#cb-frontend", p.frontend_prompt);
  set("#cb-ptimeout", p.phase_timeout || 1800);
  $("#cb-sandbox").checked = p.sandbox !== false;
  if (r.external) {
    $("#cb-target").value = "external";
    set("#cb-ext-base", r.external.base_url); set("#cb-ext-model", r.external.model);
    set("#cb-ext-name", r.external.name); set("#cb-ext-ctx", p.external && p.external.context_window);
    $("#cb-ext-key").value = "";   // キーは履歴に残らないので再入力
    cbTargetChange();
    toast("外部 API 設定を読み込みました (API キーは再入力してください)");
  } else {
    $("#cb-target").value = "profile";
    cbTargetChange();
    set("#cb-profile", r.profile_id);
    $("#cb-autostart").checked = p.auto_start !== false;
    $("#cb-stopafter").checked = !!p.stop_after;
    // モデル / thinking は選択肢ロード後に適用 (effort 段階はそのまま復元)
    S.coding._restored = {profile: r.profile_id, model: p.model || "",
                          thinking: p.thinking || "off"};
    cbLoadModelOptions();
    toast("実行フォームに読み込みました");
  }
  cbPromptCount(); cbSetTab("run"); cbPersist();
}
/* 履歴から選んでビューアーで起動 (「履歴で選択して起動」の入口) */
async function cbViewRun(id) {
  const d = S.coding;
  if (!d.selA || d.selA === id) d.selA = id;
  else if (!d.selB || d.selB === id) d.selB = id;
  else d.selB = id;
  d.codeA = d.codeB = null;
  cbSetTab("viewer"); cbPersist();
  const side = d.selA === id ? "A" : "B";
  const r = d.runs.find(x => x.id === id);
  if (r && !r.preview && !["queued", "running"].includes(r.status)) {
    try { await api(`/api/coding/runs/${id}/preview`, {method: "POST"}); }
    catch (e) { toast("プレビュー起動失敗: " + e.message); }
  }
  cbPoll(true);
}
function cbBadge(st) {
  const map = {completed: ["ok", "完了"], running: ["warn", "実行中"], queued: ["info", "待機"],
               failed: ["err", "失敗"], cancelled: ["", "中断"], lost: ["purple", "切断"]};
  const [cls, label] = map[st] || ["", st || "-"];
  return `<span class="badge ${cls}"><span class="dot"></span>${label}</span>`;
}
/* ------------------------------------------------------------- metrics 比較 */
/* フェーズ統計を実行単位の指標に集計 (古い履歴に無いフィールドは null) */
function cbAgg(r) {
  const phs = [r.phases && r.phases.backend, r.phases && r.phases.frontend].filter(Boolean);
  const sum = k => phs.reduce((a, p) => a + (p[k] || 0), 0);
  const calls = sum("tool_calls"), errs = sum("tool_errors");
  const tout = sum("tokens_out"), genSum = sum("gen_secs_sum"), genN = sum("gen_n");
  const ttftSum = sum("ttft_sum"), ttftN = sum("ttft_n");
  const turnSum = sum("turn_secs_sum"), turnN = sum("turn_n");
  return {
    secs: r.started_at != null && r.finished_at != null ? r.finished_at - r.started_at : null,
    bsecs: r.phases && r.phases.backend ? r.phases.backend.secs : null,
    fsecs: r.phases && r.phases.frontend ? r.phases.frontend.secs : null,
    turns: sum("turns"), calls, errs, errRate: calls ? errs / calls : null,
    tout, tin: sum("tokens_in"),
    genAvg: genN ? genSum / genN : null,
    turnAvg: turnN ? turnSum / turnN : null,
    ttftAvg: ttftN ? ttftSum / ttftN : null,
    tps: genSum > 0 ? tout / genSum : null,
    files: r.files ? r.files.count : 0, bytes: r.files ? r.files.bytes : 0,
  };
}
const CB_METRICS = [
  {k: "secs", label: "実装時間", fmt: v => fmtUp(v), better: "low"},
  {k: "bsecs", label: "うち backend", fmt: v => fmtUp(v), better: "low"},
  {k: "fsecs", label: "うち frontend", fmt: v => fmtUp(v), better: "low"},
  {k: "turns", label: "ターン数", fmt: v => v, better: null},
  {k: "calls", label: "ツール呼び出し", fmt: v => v, better: null},
  {k: "errs", label: "ツールミス (率)", fmt: (v, a) => `${v} (${a.errRate != null ? Math.round(a.errRate * 100) : 0}%)`, better: "low"},
  {k: "tout", label: "生成トークン", fmt: v => v, better: null},
  {k: "tin", label: "入力トークン", fmt: v => v, better: null},
  {k: "genAvg", label: "平均思考 (生成) 時間/ターン", fmt: v => v != null ? v.toFixed(2) + "s" : "-", better: "low"},
  {k: "turnAvg", label: "平均ターン時間 (ツール込み)", fmt: v => v != null ? v.toFixed(2) + "s" : "-", better: "low"},
  {k: "ttftAvg", label: "平均 TTFT", fmt: v => v != null ? v.toFixed(2) + "s" : "-", better: "low"},
  {k: "tps", label: "平均デコード速度", fmt: v => v != null ? v.toFixed(1) + " tok/s" : "-", better: "high"},
  {k: "files", label: "生成ファイル数", fmt: v => v, better: null},
];
/* 実行を列にした比較表。better=low/high の行は最良値を緑で強調 (2 件以上で差があるとき) */
function cbMetricsTable(runs, key) {
  const aggs = runs.map(cbAgg);
  const head = `<tr><th class="cb-metric">指標</th>${runs.map((r, i) =>
    `<th title="${esc(r.profile_name)} / ${esc(r.model || "")}">${esc(r.name)}${i === 0 ? ' <span class="muted">(基準)</span>' : ""}<div class="muted" style="font-weight:400">${esc(r.model || r.profile_name)}</div></th>`).join("")}</tr>`;
  const rows = CB_METRICS.map(m => {
    const vals = aggs.map(a => a[m.k]);
    let best = -1;
    if (m.better && runs.length >= 2) {
      const nn = vals.map((v, i) => [v, i]).filter(([v]) => v != null);
      if (nn.length >= 2 && new Set(nn.map(([v]) => v)).size >= 2) {
        nn.sort((a, b) => m.better === "low" ? a[0] - b[0] : b[0] - a[0]);
        best = nn[0][1];
      }
    }
    return `<tr><td class="cb-metric">${m.label}</td>${vals.map((v, i) =>
      `<td class="${i === best ? "cb-best" : ""}">${v == null ? "-" : m.fmt(v, aggs[i])}</td>`).join("")}</tr>`;
  }).join("");
  return `<div class="bm-wrap" data-scrollkey="${esc(key)}"><table class="cb-table"><thead>${head}</thead><tbody>${rows}</tbody></table></div>`;
}
function cbCmpToggle(id) {
  const d = S.coding;
  const i = d.cmp.indexOf(id);
  if (i >= 0) d.cmp.splice(i, 1);
  else { d.cmp.push(id); if (d.cmp.length > 6) d.cmp.shift(); }
  cbPersist();
  cbCmpSync();                  // チェック状態を即座に in-place 反映 (上限で押し出された行も)
  renderCodingHistory(true);    // 比較表もフォーカス中に後回しせず即時反映
}
function cbCmpClear() { S.coding.cmp = []; cbPersist(); cbCmpSync(); renderCodingHistory(true); }
/* チェックボックスの DOM 状態を S.cmp に合わせる。ポーリング描画は「フォーカス中の
   入力は巻き込まない」ガードで後回しになるため、クリック直後はここで即時に同期する
   (dataset.base も引き直して formDirty の「未保存編集」誤認を防ぐ)。 */
function cbCmpSync() {
  const rowsEl = $("#cb-history [data-rows]");
  if (!rowsEl) return;
  rowsEl.querySelectorAll("[data-row]").forEach(row => {
    const cb = row.querySelector(".cb-cmp input");
    if (!cb) return;
    cb.checked = S.coding.cmp.includes(row.dataset.row);
    cb.dataset.base = cb.checked ? "1" : "0";
  });
}
/* ------------------------------------------------------------- 出力履歴 (transcript) */
/* モデルが実際に出力したトークン内容 (text / thinking / ツール引数) を履歴から見る。
   pi の生 JSONL (message_end) をバックエンドが解析して返す。実行中の実行は
   ポーリングごとに自動更新 (ライブ閲覧)。 */
async function cbToggleTranscript(id) {
  const d = S.coding;
  if (d.tx.rid === id) {
    d.tx = {rid: "", phase: d.tx.phase, data: null, loading: false, error: ""};
    renderCodingHistory(true);
    return;
  }
  d.tx = {rid: id, phase: d.tx.phase || "backend", data: null, loading: true, error: ""};
  renderCodingHistory(true);
  await cbFetchTranscript();
  renderCodingHistory(true);   // 取得完了もユーザー操作由来: フォーカス中に後回ししない
}
function cbTxPhase(phase) {
  const d = S.coding;
  if (!d.tx.rid || d.tx.phase === phase) return;
  d.tx.phase = phase; d.tx.data = null; d.tx.loading = true; d.tx.error = "";
  renderCodingHistory(true);
  cbFetchTranscript().then(() => renderCodingHistory(true));
}
async function cbFetchTranscript() {
  const d = S.coding;
  const rid = d.tx.rid;
  if (!rid) return;
  if (!d.tx.data) d.tx.loading = true;
  try {
    const r = await api(`/api/coding/runs/${rid}/transcript?phase=${d.tx.phase}`);
    if (d.tx.rid !== rid) return;          // 取得中に別の実行へ切り替わった
    d.tx.data = r; d.tx.error = "";
  } catch (e) { if (d.tx.rid === rid) d.tx.error = e.message; }
  if (d.tx.rid === rid) d.tx.loading = false;
  renderCodingHistory();
}
async function cbTxCopy() {
  const tx = S.coding.tx;
  if (!tx.data || !tx.data.messages.length) return toast("コピーする出力がありません");
  const txt = tx.data.messages.map(m => {
    const parts = [];
    if (m.thinking) parts.push("[thinking]\n" + m.thinking);
    if (m.text) parts.push(m.text);
    for (const t of m.tools || []) parts.push(`[tool] ${t.name} ${JSON.stringify(t.args ?? {})}`);
    return `--- #${m.i} ---\n` + parts.join("\n\n");
  }).join("\n\n");
  try { await navigator.clipboard.writeText(txt); toast(`モデル出力をコピーしました (${txt.length.toLocaleString()} 文字)`); }
  catch { toast("コピー権限がありません。手動選択でお願いします"); }
}
function cbTranscriptPanel(r) {
  const tx = S.coding.tx;
  const tabs = [["backend", "バックエンド"], ["frontend", "フロントエンド"]].map(([ph, lab]) =>
    `<button class="bm-tab cb-tx-tab ${tx.phase === ph ? "on" : ""}" onclick="cbTxPhase('${ph}')">${lab}</button>`).join("");
  let body;
  if (tx.loading && !tx.data) body = `<div class="muted" style="padding:10px">読み込み中…</div>`;
  else if (tx.error) body = `<div class="muted" style="padding:10px;color:var(--err)">取得失敗: ${esc(tx.error)}</div>`;
  else if (!tx.data || tx.data.missing) body = `<div class="muted" style="padding:10px">このフェーズの出力記録はまだありません (実行前か、記録の無い古い実行です)。</div>`;
  else if (!tx.data.messages.length) body = `<div class="muted" style="padding:10px">アシスタント出力はまだありません。</div>`;
  else {
    body = tx.data.messages.map(m => {
      const ts = m.ts ? new Date(m.ts).toLocaleTimeString("ja-JP", {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "";
      const think = m.thinking ? `<details class="adv" data-detailkey="tx:${r.id}:${tx.phase}:${m.i}:th">
        <summary class="muted">思考 (${m.thinking.length.toLocaleString()} 文字)</summary>
        <div class="inner"><pre class="cb-tx-think mono">${esc(m.thinking)}</pre></div></details>` : "";
      const tools = (m.tools || []).map((t, j) => {
        const args = JSON.stringify(t.args ?? {}, null, 2);
        return `<details class="adv" data-detailkey="tx:${r.id}:${tx.phase}:${m.i}:t${j}">
          <summary class="mono">${IC("wrench")} ${esc(t.name || "?")} <span class="muted">(引数 ${args.length.toLocaleString()} 文字)</span></summary>
          <div class="inner"><pre class="cb-tx-args mono">${esc(args)}</pre></div></details>`;
      }).join("");
      const text = m.text ? `<pre class="cb-tx-text">${esc(m.text)}</pre>` : "";
      const err = m.error ? `<div class="cb-warn">${IC("alert")} ${esc(m.error)}</div>` : "";
      return `<div class="cb-tx-msg">
        <div class="muted cb-tx-hd">#${m.i} · 出力 ${m.tokens_out || 0} tok / 入力 ${m.tokens_in || 0} tok${m.stop_reason ? ` · ${esc(m.stop_reason)}` : ""}${ts ? ` · ${ts}` : ""}${m.clipped ? " · 冒頭のみ" : ""}</div>
        ${think}${text}${tools}${err}</div>`;
    }).join("");
    if (tx.data.truncated) body += `<div class="muted" style="padding:6px 10px">出力が大きいため ${tx.data.total} 件中先頭 ${tx.data.messages.length} 件のみ表示しています。</div>`;
  }
  return `<div class="cb-tx">
    <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
      <b style="font-size:12.5px">${IC("article")} モデル出力履歴</b>
      <div class="bm-tabs cb-tx-tabs">${tabs}</div>
      <span class="grow"></span>
      <button class="ghost" onclick="cbTxCopy()" style="min-height:30px;padding:3px 9px;font-size:11.5px">${IC("clipboard")} コピー</button>
      <button class="ghost" onclick="cbToggleTranscript('${r.id}')" style="min-height:30px;padding:3px 9px;font-size:11.5px">${IC("chev_up")} 閉じる</button>
    </div>
    <div class="cb-tx-body" data-logkey="tx:${r.id}:${tx.phase}">${body}</div>
  </div>`;
}
/* ------------------------------------------------------------- live card */
async function cbToggleLog() {
  S.coding.logOpen = !S.coding.logOpen;
  if (S.coding.logOpen) await cbFetchLog();
  renderCoding();
}
async function cbFetchLog() {
  const d = S.coding;
  const run = d.runs.find(r => r.id === d.active);
  const phase = run && ["backend", "frontend"].includes(run.progress.phase)
    ? run.progress.phase : d.logPhase;
  d.logPhase = phase;
  try {
    const r = await api(`/api/coding/runs/${d.active}/log?phase=${phase}`);
    d.logText = (r.log || "") + (r.stderr ? "\n--- stderr ---\n" + r.stderr : "");
  } catch { d.logText = "(ログ取得失敗)"; }
}
function renderCodingActive(run) {
  const el = $("#cb-active");
  if (!el) return;
  if (!run) {
    /* 完了で active が null になったらカードを消す。setHtml のキャッシュは使わない:
       ライブカードは innerHTML 直書きでキャッシュと同期していないため、setHtml("") が
       「変更なし」と誤認して完了済みカードが残る (手動更新まで実行タブに残る事故)。 */
    const redo = () => renderCodingActive(S.coding.runs.find(r => r.id === S.coding.active) || null);
    if (el.childElementCount && (uiScrolling() || uiBusyIn(el))) { deferRender("cb-act", redo); return; }
    if (el.childElementCount) { el.innerHTML = ""; S.coding._act = ""; }
    return;
  }
  const html = cbActiveCard(run);
  // ライブ更新は実行中だけ。完了済みカードは内容が変わらない限り組み直さない
  if (html === S.coding._act && el.childElementCount) return;
  /* 骨格が変わらなければライブ欄 (経過 / カウンタ / フェーズ / 状態) だけ in-place。
     カード内でテキスト選択していてもライブ更新は止まらない (選択中のノードは
     書き換えないので選択も解除されない)。全体組み直しは骨格変更時だけ。 */
  if (el.childElementCount && cbActivePatch(el, run, html)) { S.coding._act = html; return; }
  // 骨格変更 (実行切替 / ログ開閉 / 警告・ライブテキストの出現): 操作中なら後回し
  if (el.childElementCount && (uiScrolling() || uiBusyIn(el))) { deferRender("cb-act", () => renderCodingActive(run)); return; }
  S.coding._act = html;
  el._struct = cbActiveStruct(run);
  el.innerHTML = html;
}
/* 骨格シグネチャ: 要素の有無 / 固定文字列を変えるフィールドだけ。これが同じなら
   残りは数値のみ = data-f フックで in-place 書き換えできる。 */
function cbActiveStruct(run) {
  const p = run.progress || {};
  return JSON.stringify([run.id, run.status, run.warning || "", S.coding.logOpen,
    !!p.tool_errors, p.gen_avg != null, !!p.last_tool, !!p.text,
    run.model || "", run.profile_name, !!(run.params && run.params.sandbox === false)]);
}
function cbActivePatch(el, run, html) {
  if (el._struct !== cbActiveStruct(run)) return false;
  patchFields(el, html);
  return true;
}
/* 実行中カードの HTML (完了後は固定 = キャッシュで書き換えスキップ) */
function cbActiveCard(run) {
  const p = run.progress || {};
  const steps = ["launching", "waiting", "backend", "frontend", "scanning"];
  const idx = steps.indexOf(p.phase);
  const stepHtml = steps.map((s, i) => {
    const cls = i < idx ? "ok" : i === idx ? "warn" : "";
    return `<span class="badge ${cls}"><span class="dot"></span>${CB_PHASES[s] || s}</span>`;
  }).join(`<span class="muted">→</span>`);
  return `
  <div class="card cb-live">
    <div class="row">
      <b class="grow ellip">${esc(run.name)}</b>
      <span data-f="badge">${cbBadge(run.status)}</span>
      <button class="danger" onclick="cbCancel('${run.id}')" style="min-height:34px;padding:5px 12px">${IC("stop")} 中断</button>
    </div>
    <div class="row" data-f="steps" style="gap:6px;margin-top:8px">${stepHtml}</div>
    <div class="row" style="gap:14px;margin-top:8px;flex-wrap:wrap">
      <span class="muted">ターン <b data-f="turns">${p.turns || 0}</b></span>
      <span class="muted">ツール <b data-f="tools">${p.tool_calls || 0}</b></span>
      ${p.tool_errors ? `<span class="muted" style="color:var(--err)">ツールエラー <b data-f="errors">${p.tool_errors}</b></span>` : ""}
      <span class="muted">tokens <b data-f="tokens">${(p.tokens_in || 0) + (p.tokens_out || 0)}</b> <span data-f="tokio">(in ${p.tokens_in || 0} / out ${p.tokens_out || 0})</span></span>
      <span class="muted">経過 <b data-f="elapsed">${fmtUp(p.elapsed || 0)}</b></span>
      ${p.gen_avg != null ? `<span class="muted">平均思考 <b data-f="genavg">${p.gen_avg}s</b>/ターン</span>` : ""}
      ${p.last_tool ? `<span class="badge info" data-f="lasttool">${IC("wrench")} ${esc(p.last_tool)}</span>` : ""}
    </div>
    ${run.warning ? `<div class="cb-warn">${IC("alert")} ${esc(run.warning)}</div>` : ""}
    ${p.text ? `<div class="cb-live-text muted" data-f="text">${esc(p.text)}</div>` : ""}
    <div class="row" style="margin-top:8px">
      <button class="ghost" onclick="cbToggleLog()" style="min-height:32px;padding:4px 10px">${IC("article")} ${S.coding.logOpen ? "ログを隠す" : "pi ログを見る"}</button>
      <span class="muted" style="font-size:11px">${esc(run.profile_name)} / ${esc(run.model || "(モデル解決中)")} / ${run.params && run.params.sandbox === false ? "サンドボックス OFF" : "サンドボックス内"}</span>
    </div>
    ${S.coding.logOpen ? `<pre class="cb-log mono">${esc(S.coding.logText || "(まだログがありません)")}</pre>` : ""}
  </div>`;
}
/* ------------------------------------------------------------- history */
function renderCodingHistory(force) {
  const el = $("#cb-history");
  if (!el) return;
  const runs = S.coding.runs;
  $("#cbt-hn").textContent = runs.length;
  if (uiScrolling() && el.childElementCount) { deferRender("cb-hist", renderCodingHistory); return; }
  // 選択 / チェックボックス操作中 / アニメーション中は組み直しを後回し。
  // force = ユーザーのチェック操作由来: フォーカス中でも即時に組み直す
  if (!force && uiBusyIn(el) && el.childElementCount) { deferRender("cb-hist", renderCodingHistory); return; }
  if (!runs.length) {
    const empty = `<div class="card muted">まだ実行履歴がありません。実行タブからコーディングベンチを開始してください。</div>`;
    if (S.coding._hist !== empty) { S.coding._hist = empty; el.innerHTML = empty; }   // 空欄も毎回は書き直さない
    return;
  }
  const rows = runs.map(r => {
    const secs = r.started_at != null && r.finished_at != null ? r.finished_at - r.started_at : null;
    const ph = r.phases || {};
    const tools = (ph.backend ? ph.backend.tool_calls : 0) + (ph.frontend ? ph.frontend.tool_calls : 0);
    const terr = (ph.backend ? ph.backend.tool_errors || 0 : 0) + (ph.frontend ? ph.frontend.tool_errors || 0 : 0);
    const toks = (ph.backend ? ph.backend.tokens_out : 0) + (ph.frontend ? ph.frontend.tokens_out : 0);
    const a = cbAgg(r);
    const viewable = !["queued", "running"].includes(r.status);
    const checked = S.coding.cmp.includes(r.id);
    return `
    <div class="card cb-hrow" data-row="${esc(r.id)}">
      <div class="row">
        <label class="cb-cmp" title="比較表に追加 (最大 6 件)"><input type="checkbox" ${checked ? "checked" : ""} onchange="cbCmpToggle('${r.id}')"><span>比較</span></label>
        <b class="grow ellip">${esc(r.name)}</b>
        ${cbBadge(r.status)}
        ${r.preview ? `<span class="badge ok"><span class="dot"></span>プレビュー中</span>` : ""}
      </div>
      <div class="muted" style="margin-top:4px">${esc(r.profile_name)} [${esc(r.backend_name || r.backend)}] / ${esc(r.model || "-")} / ${fmtDT(r.created_at)}</div>
      <div class="row" style="gap:12px;margin-top:4px;flex-wrap:wrap">
        <span class="muted">ファイル <b>${r.files ? r.files.count : 0}</b></span>
        <span class="muted">ツール <b>${tools}</b></span>
        ${terr ? `<span class="muted" style="color:var(--err)">ツールミス <b>${terr}</b></span>` : ""}
        <span class="muted">出力tok <b>${toks}</b></span>
        <span class="muted">所要 <b>${secs != null ? fmtUp(secs) : "-"}</b></span>
        ${a.genAvg != null ? `<span class="muted">平均思考 <b>${a.genAvg.toFixed(2)}s</b>/ターン</span>` : ""}
        ${a.tps != null ? `<span class="muted">デコード <b>${a.tps.toFixed(1)}</b> tok/s</span>` : ""}
        ${r.error ? `<span class="muted" style="color:var(--err)">${esc(r.error.slice(0, 120))}</span>` : ""}
      </div>
      ${r.warning ? `<div class="cb-warn">${IC("alert")} ${esc(r.warning)}</div>` : ""}
      <div class="row" style="gap:6px;margin-top:8px">
        <button class="primary" ${viewable ? "" : "disabled"} onclick="cbViewRun('${r.id}')" style="min-height:34px;padding:5px 12px">${IC("layers")} 見て比較</button>
        <button class="ghost" onclick="cbToggleTranscript('${r.id}')" style="min-height:34px;padding:5px 12px">${IC("article")} 出力${S.coding.tx.rid === r.id ? " ▴" : ""}</button>
        <button class="ghost" onclick="cbRerun('${r.id}')" style="min-height:34px;padding:5px 12px">${IC("refresh")} 再実行</button>
        <button class="ghost" onclick="cbDelete('${r.id}')" style="min-height:34px;padding:5px 12px;color:var(--err)">${IC("trash")} 削除</button>
      </div>
      ${S.coding.tx.rid === r.id ? cbTranscriptPanel(r) : ""}
    </div>`;
  }).join("");
  const cmpRuns = S.coding.cmp.map(id => runs.find(r => r.id === id)).filter(Boolean);
  const cmpPanel = cmpRuns.length >= 2 ? `
    <div class="card">
      <div class="row" style="margin-bottom:6px">
        <b class="grow">${IC("chart")} メトリクス比較 (${cmpRuns.length} 件・先頭が基準)</b>
        <button class="ghost" onclick="cbCmpClear()" style="min-height:32px;padding:4px 10px">クリア</button>
      </div>
      ${cbMetricsTable(cmpRuns, "cbm:hist")}
      <div class="muted" style="font-size:11px;margin-top:6px">緑 = その行の最良値 (時間・ミスは少ない方、デコード速度は速い方)。ツールミス = pi の引数検証エラー等。平均思考時間 = アシスタント応答の生成開始〜完了の平均。</div>
    </div>` : "";
  const head = `
    <div class="row" style="margin-bottom:8px">
      <span class="muted grow">${runs.length} 件 (新しい順) ・「比較」チェックで最大 6 件までメトリクス比較表</span>
      <button class="ghost" onclick="cbClearAll()" style="color:var(--err)">${IC("trash")} 完了分を一括削除</button>
    </div>`;
  const html = head + cmpPanel + rows;
  // 中身が変わらなければ組み直さない (2.5 秒ごとの選択解除・チェック飛びを防ぐ)
  if (html === S.coding._hist && el.childElementCount) return;
  S.coding._hist = html;
  // 見出し / 比較表 / 行を分離: 変わった行だけ組み直す (patchRows)
  if (!el.querySelector("[data-rows]")) el.innerHTML = `<div data-head></div><div data-cmp></div><div data-rows></div>`;
  setHtml(el.querySelector("[data-head]"), head, "cb-hist-head", renderCodingHistory);
  setHtml(el.querySelector("[data-cmp]"), cmpPanel, "cb-hist-cmp", renderCodingHistory);
  patchRows(el.querySelector("[data-rows]"), rows, "cb-hist", renderCodingHistory);
}
/* ------------------------------------------------------------- viewer (2 実装並列) */
function cbSel(side, rid) {
  const d = S.coding;
  d["sel" + side] = rid;
  d["code" + side] = null;
  cbPersist(); renderCodingViewer();
}
function cbSwap() {
  const d = S.coding;
  [d.selA, d.selB] = [d.selB, d.selA];
  [d.modeA, d.modeB] = [d.modeB, d.modeA];
  [d.codeA, d.codeB] = [d.codeB, d.codeA];
  const a = $("#cb-selA"), b = $("#cb-selB");
  if (a) a.value = d.selA;
  if (b) b.value = d.selB;
  cbPersist(); renderCodingViewer();
}
function cbMode(side, m) {
  const d = S.coding;
  d["mode" + side] = m;
  d["code" + side] = null;
  renderCodingViewer();
  if (m === "files") cbEnsureFiles(side);
}
async function cbEnsureFiles(side) {
  const d = S.coding;
  const rid = d["sel" + side];
  if (!rid || (d["files" + side] && d._fv[side] === rid)) return;
  try {
    const r = await api(`/api/coding/runs/${rid}/files`);
    d["files" + side] = r.files || [];
    d._fv[side] = rid;
  } catch (e) { toast("ファイル一覧の取得失敗: " + e.message); d["files" + side] = []; }
  renderCodingViewer();
}
async function cbPickFile(side, path) {
  const d = S.coding;
  const rid = d["sel" + side];
  try {
    d["code" + side] = await api(`/api/coding/runs/${rid}/file?path=${encodeURIComponent(path)}`);
    d._codeRev++;
  } catch (e) { toast("ファイル取得失敗: " + e.message); }
  renderCodingViewer();
}
function cbCloseCode(side) {
  S.coding["code" + side] = null;
  renderCodingViewer();
}
async function cbPreviewStart(side) {
  const rid = S.coding["sel" + side];
  if (!rid) return;
  try { await api(`/api/coding/runs/${rid}/preview`, {method: "POST"}); }
  catch (e) { toast("プレビュー起動失敗: " + e.message); }
  cbPoll(true);
}
async function cbPreviewStop(side) {
  const rid = S.coding["sel" + side];
  if (!rid) return;
  try { await api(`/api/coding/runs/${rid}/preview`, {method: "DELETE"}); }
  catch (e) { toast("プレビュー停止失敗: " + e.message); }
  cbPoll(true);
}
function cbReload(side) {
  const f = $(`#cb-frame${side}`);
  if (!f) return;
  try { f.contentWindow.location.reload(); }
  catch { f.src = f.src; }
}
function cbViewerOptions(selected) {
  const runs = S.coding.runs.filter(r => !["queued", "running"].includes(r.status));
  return `<option value="">(実行を選択)</option>` + runs.map(r =>
    `<option value="${r.id}" ${r.id === selected ? "selected" : ""}>${esc(r.name)} — ${esc(r.profile_name)} (${fmtDT(r.created_at)})</option>`).join("");
}
function cbPane(side) {
  const d = S.coding;
  const rid = d["sel" + side];
  const run = d.runs.find(r => r.id === rid);
  const mode = d["mode" + side];
  if (!run) {
    return `<div class="cb-pane-hd"><b>実装 ${side}</b></div>
      <div class="cb-ph muted">上のドロップダウンから実行履歴を選択してください。<br>履歴タブの「見て比較」からも選べます。</div>`;
  }
  const pv = run.preview;
  const running = pv && (pv.mode === "static" || pv.port);
  const ph = run.phases || {};
  const tools = (ph.backend ? ph.backend.tool_calls : 0) + (ph.frontend ? ph.frontend.tool_calls : 0);
  const secs = run.started_at != null && run.finished_at != null ? run.finished_at - run.started_at : null;
  const stats = `<div class="muted cb-stats">${esc(run.profile_name)} / ${esc(run.model || "-")} ・
    ツール ${tools} ・ 出力tok ${(ph.backend ? ph.backend.tokens_out : 0) + (ph.frontend ? ph.frontend.tokens_out : 0)} ・
    ${secs != null ? fmtUp(secs) : "-"} ・ ファイル ${run.files ? run.files.count : 0}</div>`;
  let toolbar = `<div class="cb-toolbar">`;
  if (mode === "preview") {
    toolbar += running
      ? `<button onclick="cbPreviewStop('${side}')" style="min-height:32px;padding:4px 10px;color:var(--err)">${IC("stop")} 停止</button>
         <button onclick="cbReload('${side}')" style="min-height:32px;padding:4px 10px">${IC("refresh")} 再読込</button>
         <span class="badge ok"><span class="dot"></span>${pv.mode === "static" ? "静的配信中" : "ポート " + pv.port}</span>`
      : `<button class="primary" onclick="cbPreviewStart('${side}')" style="min-height:32px;padding:4px 10px">${IC("play")} 起動</button>
         <span class="muted" style="font-size:11px">生成アプリをサンドボックス内で起動して表示</span>`;
  } else {
    toolbar += `<button onclick="cbMode('${side}','preview')" style="min-height:32px;padding:4px 10px">${IC("monitor")} プレビューへ</button>`;
    if (d["code" + side]) toolbar += `<button onclick="cbCloseCode('${side}')" style="min-height:32px;padding:4px 10px">${IC("chev_up")} 一覧へ</button>`;
  }
  toolbar += `<span class="grow"></span>
    <select onchange="cbMode('${side}', this.value)" style="min-height:32px;padding:2px 8px">
      <option value="preview" ${mode === "preview" ? "selected" : ""}>プレビュー</option>
      <option value="files" ${mode === "files" ? "selected" : ""}>ファイル</option>
    </select></div>`;
  let body;
  if (mode === "preview") {
    if (running) {
      const src = pv.mode === "static"
        ? `/api/coding/runs/${rid}/static/`
        : `/api/coding/runs/${rid}/proxy/`;
      body = `<iframe id="cb-frame${side}" class="cb-frame" src="${src}" title="実装 ${side} のプレビュー"
        sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-modals"></iframe>`;
    } else {
      body = `<div class="cb-ph muted">プレビューは停止中です。<br>「起動」を押すと生成アプリ (backend/server.py 等) を実行します。<br>
        バックエンドが無い実装は frontend/ を静的配信します。</div>`;
    }
  } else {
    const code = d["code" + side];
    if (code) {
      body = `<div class="cb-code-hd mono ellip">${esc(code.path)} ${code.truncated ? '<span class="badge warn">先頭 512KB のみ</span>' : ""}</div>
        <pre class="cb-code mono">${esc(code.text)}</pre>`;
    } else {
      const files = d["files" + side];
      body = files == null
        ? `<div class="cb-ph muted">読み込み中…</div>`
        : files.length === 0
          ? `<div class="cb-ph muted">ファイルがありません</div>`
          : `<div class="cb-files">` + files.map(f =>
              `<button class="cb-file mono" onclick="cbPickFile('${side}', '${esc(f.path)}')">${IC("file")} ${esc(f.path)} <span class="muted">${f.size} B</span></button>`).join("") + `</div>`;
    }
  }
  return `
    <div class="cb-pane-hd"><b class="grow ellip">${esc(run.name)}</b> ${cbBadge(run.status)}</div>
    ${stats}${toolbar}<div class="cb-body">${body}</div>`;
}
function renderCodingViewer() {
  const el = $("#cb-viewer");
  if (!el) return;
  const d = S.coding;
  const viewable = d.runs.filter(r => !["queued", "running"].includes(r.status));
  if (!viewable.length) {
    setHtml(el, `<div class="card muted">まだ比較できる実行がありません。実行タブでコーディングベンチを完了させてください。</div>`, "cb-viewer", renderCodingViewer);
    return;
  }
  // 骨格 (選択行 + 2 ペイン) は一度だけ作る。ペインごとに差分描画するので、
  // A を変更しても B の iframe は読み直されない (ポーリングでも壊れない)。
  if (!$("#cb-selA") || !$("#cb-paneA")) {
    el.innerHTML = `
    <div class="row cb-selrow">
      <div class="grow"><label class="f">実装 A</label><select id="cb-selA" onchange="cbSel('A', this.value)"></select></div>
      <button class="ghost" onclick="cbSwap()" title="A/B 入れ替え" style="align-self:flex-end">${IC("refresh")} 入替</button>
      <div class="grow"><label class="f">実装 B</label><select id="cb-selB" onchange="cbSel('B', this.value)"></select></div>
    </div>
    <div id="cb-vtable"></div>
    <div class="cb-grid"><div class="cb-pane" id="cb-paneA"></div><div class="cb-pane" id="cb-paneB"></div></div>
    <div class="muted" style="margin-top:8px;font-size:11.5px">プレビューは launcher 経由で中継されるため LAN のスマホからも表示できます。生成アプリはサンドボックス内で起動しますが、ブラウザ上では launcher と同一オリジンで動く点に注意してください。</div>`;
  }
  // 選択肢は開いている (フォーカスがある) 間組み直さない = 選択が飛ばない
  setOptions($("#cb-selA"), cbViewerOptions(d.selA), "cb-selA", renderCodingViewer);
  setOptions($("#cb-selB"), cbViewerOptions(d.selB), "cb-selB", renderCodingViewer);
  // A/B 両方選択中はメトリクス比較表をペーンの上に出す (iframe には触らない)
  const vt = $("#cb-vtable");
  if (vt) {
    const ra = d.runs.find(r => r.id === d.selA), rb = d.runs.find(r => r.id === d.selB);
    const sig = (ra ? ra.id + ":" + (ra.finished_at || 0) : "") + "|" + (rb ? rb.id + ":" + (rb.finished_at || 0) : "");
    if (vt._sig !== sig) {
      vt._sig = sig;
      vt.innerHTML = ra && rb ? `<div class="card" style="padding:10px 12px">${cbMetricsTable([ra, rb], "cbm:viewer")}</div>` : "";
    }
  }
  ["A", "B"].forEach(side => {
    const rid = d["sel" + side];
    const run = d.runs.find(r => r.id === rid);
    const pv = run && run.preview ? (run.preview.mode + ":" + (run.preview.port || "")) : "";
    const code = d["code" + side];
    const files = d["files" + side];
    const sig = JSON.stringify([rid, d["mode" + side], pv, run ? run.status : "",
      code ? code.path : "", d._codeRev, d._fv[side], files ? files.length : -1]);
    if (sig !== d["_psig" + side]) {
      const paneEl = $("#cb-pane" + side);
      // プレビュー iframe を操作中 / コードを選択中はペインの組み直しを後回し
      if (uiBusyIn(paneEl) && paneEl.childElementCount) { deferRender("cb-pane" + side, renderCodingViewer); return; }
      d["_psig" + side] = sig;
      paneEl.innerHTML = cbPane(side);
    }
  });
  if (d.modeA === "files") cbEnsureFiles("A");
  if (d.modeB === "files") cbEnsureFiles("B");
}
/* ------------------------------------------------------------- AI 評価 (2 実行の定量診断) */
/* 完了済み 2 実行のメトリクス + 実装ファイルを judge モデル (稼働中プロファイル /
   外部 API) に渡し、軸別スコア・欠陥・総合判定を JSON で得る。ビューアーと同じ
   A/B 選択 (selA/selB) を使う。評価自体も履歴として保存・閲覧できる。 */
const CB_AXES = [
  {k: "requirements", label: "要件充足"},
  {k: "correctness", label: "正確性・堅牢性"},
  {k: "code_quality", label: "コード品質"},
  {k: "frontend", label: "フロントエンド品質"},
  {k: "verification", label: "自己検証"},
  {k: "efficiency", label: "効率"},
];
function cbEvalSel(side, rid) {
  S.coding["sel" + side] = rid;
  cbPersist(); renderCodingEval();
}
function cbEvalSwap() {
  const d = S.coding;
  [d.selA, d.selB] = [d.selB, d.selA];
  cbPersist(); renderCodingEval();
}
function cbEvalJudgeSync() {
  const d = S.coding;
  d.judgeKind = $("#cb-ev-kind").value;
  d.judgePid = $("#cb-ev-pid") ? $("#cb-ev-pid").value : d.judgePid;
  d.judgeExtBase = $("#cb-ev-base") ? $("#cb-ev-base").value : d.judgeExtBase;
  d.judgeExtModel = $("#cb-ev-model") ? $("#cb-ev-model").value : d.judgeExtModel;
  d.judgeThinking = $("#cb-ev-thinking").value;
  cbPersist(); renderCodingEval();
}
async function cbEvalStart() {
  const d = S.coding;
  if (!d.selA || !d.selB || d.selA === d.selB) { toast("異なる 2 つの実行を選択してください"); return; }
  const judge = {};
  if (d.judgeKind === "external") {
    if (!d.judgeExtBase.trim()) { toast("judge の Base URL を入力してください"); return; }
    judge.external = {base_url: d.judgeExtBase.trim(), api_key: $("#cb-ev-key").value,
                      model: d.judgeExtModel.trim()};
  } else {
    if (!d.judgePid) { toast("judge プロファイルを選択してください"); return; }
    judge.profile_id = d.judgePid;
  }
  d.evalBusy = true; renderCodingEval();
  try {
    await api("/api/coding/evals", {method: "POST", body: {
      run_a: d.selA, run_b: d.selB, judge, thinking: d.judgeThinking}});
    toast("AI 評価を開始しました (完了まで数分かかります)");
    await cbPoll(true);
  } catch (e) { toast("評価開始失敗: " + e.message); }
  finally { d.evalBusy = false; renderCodingEval(); }
}
async function cbEvalDelete(id) {
  if (!confirm("この評価履歴を削除しますか?")) return;
  try { await api(`/api/coding/evals/${id}`, {method: "DELETE"}); toast("削除しました"); }
  catch (e) { toast("削除失敗: " + e.message); }
  cbPoll(true);
}
function cbEvalToggle(id) {
  S.coding.evalOpen = S.coding.evalOpen === id ? "" : id;
  renderCodingEval();
}
function cbEvalBar(v, side, best) {
  return `<div class="cb-ev-bar ${best === side ? "cb-best" : ""}">
    <div class="cb-ev-fill ${side}" style="width:${Math.max(0, Math.min(100, v))}%"></div>
    <span class="cb-ev-num">${v}</span></div>`;
}
function cbEvalCard(ev) {
  const d = S.coding;
  const open = d.evalOpen === ev.id;
  const res = ev.result;
  const winner = res && res.verdict ? res.verdict.winner : "";
  const wBadge = winner === "A" ? `<span class="badge ok"><span class="dot"></span>A 勝ち</span>`
    : winner === "B" ? `<span class="badge ok"><span class="dot"></span>B 勝ち</span>`
    : winner === "tie" ? `<span class="badge info"><span class="dot"></span>引き分け</span>` : "";
  const stBadge = ev.status === "running" ? `<span class="badge warn"><span class="dot"></span>評価中…</span>`
    : ev.status === "failed" ? `<span class="badge err"><span class="dot"></span>失敗</span>`
    : `<span class="badge ok"><span class="dot"></span>完了</span>`;
  let body = "";
  if (ev.status === "failed") body = `<div class="cb-warn">${IC("alert")} ${esc(ev.error || "不明なエラー")}</div>`;
  if (res && open) {
    const scoreRows = CB_AXES.map(ax => {
      const s = res.scores[ax.k] || {a: 0, b: 0, reason: ""};
      const best = s.a === s.b ? "" : (s.a > s.b ? "a" : "b");
      return `<tr><td class="cb-metric">${ax.label}</td>
        <td>${cbEvalBar(s.a, "a", best)}</td><td>${cbEvalBar(s.b, "b", best)}</td>
        <td class="muted" style="font-size:11.5px">${esc(s.reason || "")}</td></tr>`;
    }).join("");
    const totalA = CB_AXES.reduce((t, ax) => t + ((res.scores[ax.k] || {}).a || 0), 0);
    const totalB = CB_AXES.reduce((t, ax) => t + ((res.scores[ax.k] || {}).b || 0), 0);
    const defects = side => (res.defects[side] || []).map(df =>
      `<div class="cb-ev-defect"><span class="badge ${df.severity === "high" ? "err" : df.severity === "medium" ? "warn" : ""}">${esc(df.severity)}</span>
       <span class="mono">${esc(df.file || "-")}</span> — ${esc(df.detail)}</div>`).join("")
      || `<div class="muted">欠陥の指摘なし</div>`;
    const highlights = side => (res.highlights[side] || []).map(h => `<li>${esc(h)}</li>`).join("");
    body = `
    <div class="bm-wrap" data-scrollkey="ev:${esc(ev.id)}:scores"><table class="cb-table cb-ev-table">
      <thead><tr><th class="cb-metric">評価軸 (0-100)</th><th>A: ${esc(ev.run_a.name)}</th><th>B: ${esc(ev.run_b.name)}</th><th>根拠</th></tr></thead>
      <tbody>${scoreRows}
      <tr><td class="cb-metric"><b>合計 (参考)</b></td><td><b>${totalA}</b> / 600</td><td><b>${totalB}</b> / 600</td><td></td></tr></tbody>
    </table></div>
    ${res.verdict ? `<div class="cb-ev-verdict">${wBadge} <span class="muted">確信度 ${res.verdict.confidence}%</span> — ${esc(res.verdict.reason || "")}</div>` : ""}
    ${res.summary ? `<div class="cb-ev-summary">${esc(res.summary)}</div>` : ""}
    <div class="cb-grid" style="margin-top:8px">
      <div><b style="font-size:12px">A の欠陥</b>${defects("a")}
        ${highlights("a") ? `<b style="font-size:12px">A の長所</b><ul class="cb-ev-hl">${highlights("a")}</ul>` : ""}</div>
      <div><b style="font-size:12px">B の欠陥</b>${defects("b")}
        ${highlights("b") ? `<b style="font-size:12px">B の長所</b><ul class="cb-ev-hl">${highlights("b")}</ul>` : ""}</div>
    </div>
    <details class="adv" data-detailkey="ev:${ev.id}:raw"><summary class="muted">judge 生出力 (JSON)</summary>
      <div class="inner"><pre class="cb-code mono">${esc(ev.raw || "")}</pre></div></details>`;
  } else if (res && !open) {
    const totalA = CB_AXES.reduce((t, ax) => t + ((res.scores[ax.k] || {}).a || 0), 0);
    const totalB = CB_AXES.reduce((t, ax) => t + ((res.scores[ax.k] || {}).b || 0), 0);
    body = `<div class="muted" style="margin-top:4px">合計スコア A <b>${totalA}</b> / B <b>${totalB}</b> (600 満点) ・クリックで軸別スコア・欠陥・判定を表示</div>`;
  }
  const mats = ev.materials
    ? `<span class="muted">材料: A ${ev.materials.a.files} ファイル / B ${ev.materials.b.files} ファイル${ev.materials.a.truncated || ev.materials.b.truncated ? " (切り詰めあり)" : ""}</span>` : "";
  return `
  <div class="card cb-hrow" data-row="${esc(ev.id)}">
    <div class="row" style="cursor:pointer" onclick="cbEvalToggle('${ev.id}')">
      <b class="grow ellip">${esc(ev.run_a.name)} ⇔ ${esc(ev.run_b.name)}</b>
      ${wBadge}${stBadge}
    </div>
    <div class="muted" style="margin-top:4px">judge: ${esc(ev.judge.name)}${ev.judge.model ? " / " + esc(ev.judge.model) : ""}
      [${ev.judge.kind === "external" ? "外部 API" : "プロファイル"}] ・ thinking ${esc(ev.thinking)} ・ ${fmtDT(ev.created_at)}
      ${ev.secs != null ? " ・ " + fmtUp(ev.secs) : ""}</div>
    <div class="row" style="gap:10px;margin-top:2px">${mats}</div>
    ${body}
    <div class="row" style="gap:6px;margin-top:8px">
      <button class="ghost" onclick="cbEvalToggle('${ev.id}')" style="min-height:32px;padding:4px 10px">${IC("article")} ${open ? "閉じる" : "詳細"}</button>
      <button class="ghost" onclick="cbEvalDelete('${ev.id}')" style="min-height:32px;padding:4px 10px;color:var(--err)">${IC("trash")} 削除</button>
    </div>
  </div>`;
}
function renderCodingEval() {
  const el = $("#cb-eval");
  if (!el) return;
  const d = S.coding;
  if (uiBusyIn(el) && el.childElementCount) { deferRender("cb-eval", renderCodingEval); return; }
  const ra = d.runs.find(r => r.id === d.selA), rb = d.runs.find(r => r.id === d.selB);
  const opts = cbViewerOptions(d.selA);
  const profs = S.state ? (S.state.profiles || []) : [];
  const judgeForm = d.judgeKind === "external" ? `
    <div class="row" style="gap:8px;flex-wrap:wrap;margin-top:6px">
      <div class="grow" style="min-width:200px"><label class="f">Base URL</label>
        <input type="text" id="cb-ev-base" class="mono" value="${esc(d.judgeExtBase)}" placeholder="https://api.openai.com/v1" onchange="cbEvalJudgeSync()" style="width:100%"></div>
      <div class="grow" style="min-width:160px"><label class="f">API キー</label>
        <input type="password" id="cb-ev-key" class="mono" autocomplete="off" placeholder="sk-..." style="width:100%"></div>
      <div class="grow" style="min-width:150px"><label class="f">モデル名 (空 = 先頭)</label>
        <input type="text" id="cb-ev-model" class="mono" value="${esc(d.judgeExtModel)}" onchange="cbEvalJudgeSync()" style="width:100%"></div>
    </div>` : `
    <div class="row" style="gap:8px;margin-top:6px">
      <select id="cb-ev-pid" class="grow" onchange="cbEvalJudgeSync()">
        <option value="">(judge プロファイルを選択 — 起動済みのもの)</option>
        ${profs.map(p => `<option value="${p.id}" ${p.id === d.judgePid ? "selected" : ""}>${esc(p.name)} ${liveOf(p.id) ? "● 起動中" : ""}</option>`).join("")}
      </select>
    </div>`;
  const html = `
  <div class="card">
    <div class="row cb-selrow">
      <div class="grow"><label class="f">実装 A</label><select id="cb-ev-selA" onchange="cbEvalSel('A', this.value)">${opts}</select></div>
      <button class="ghost" onclick="cbEvalSwap()" title="A/B 入れ替え" style="align-self:flex-end">${IC("refresh")} 入替</button>
      <div class="grow"><label class="f">実装 B</label><select id="cb-ev-selB" onchange="cbEvalSel('B', this.value)">${cbViewerOptions(d.selB)}</select></div>
    </div>
    ${ra && rb ? `<div style="margin-top:8px">${cbMetricsTable([ra, rb], "cbm:eval")}</div>
      <div class="muted" style="font-size:11px;margin-top:4px">上の定量メトリクスに加え、実装ファイルの内容を AI が診断します (要件充足・正確性・品質・検証・効率を 0-100 で採点)。</div>`
      : `<div class="muted" style="margin-top:6px">完了済みの実行を 2 つ選ぶと、定量メトリクス比較と AI 診断の準備ができます。ビューアーと同じ選択が共有されます。</div>`}
    <div style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px">
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <label class="f" style="margin:0">judge モデル</label>
        <select id="cb-ev-kind" onchange="cbEvalJudgeSync()">
          <option value="profile" ${d.judgeKind !== "external" ? "selected" : ""}>起動プロファイル (ローカル)</option>
          <option value="external" ${d.judgeKind === "external" ? "selected" : ""}>外部 API</option>
        </select>
        <label class="f" style="margin:0">thinking</label>
        <select id="cb-ev-thinking" onchange="cbEvalJudgeSync()">
          ${["off", "minimal", "low", "medium", "high", "xhigh", "max"].map(l =>
            `<option value="${l}" ${d.judgeThinking === l ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        <span class="muted" style="font-size:11px">judge の対応段階に従います (非対応値はエラーまたはサーバー側で丸め)</span>
      </div>
      ${judgeForm}
      <div class="row" style="margin-top:8px">
        <button class="primary" id="cb-ev-start" onclick="cbEvalStart()" ${d.evalBusy || !ra || !rb || d.selA === d.selB ? "disabled" : ""}>
          <svg class=ic aria-hidden=true><use href=#i-chart /></svg> AI で定量評価</button>
        <span class="muted" style="font-size:11px">実装内容は 1 実行あたり最大 160K 文字まで judge に渡されます。ローカル judge は GPU 実走ロックを共有します。</span>
      </div>
    </div>
  </div>
  <div class="row" style="margin:10px 0 6px"><b class="grow">${IC("article")} 評価履歴 (${d.evals.length})</b></div>
  ${d.evals.length ? d.evals.map(cbEvalCard).join("") : `<div class="card muted">まだ AI 評価がありません。2 つの実行を選んで「AI で定量評価」を押してください。</div>`}`;
  if (html === d._eval && el.childElementCount) return;
  d._eval = html;
  el.innerHTML = html;
}
/* ------------------------------------------------------------- dispatch */
function renderCoding() {
  const d = S.coding;
  if (!d) return;
  CB_TABS.forEach(t => {
    const b = $(`#cbt-${t}`), p = $(`#cbt-p-${t}`);
    if (b) { b.classList.toggle("on", d.tab === t); b.setAttribute("aria-selected", d.tab === t ? "true" : "false"); }
    if (p) p.classList.toggle("hidden", d.tab !== t);
  });
  $("#cbt-run-dot").classList.toggle("hidden", !d.active);
  /* プロファイル選択肢 (起動タブと同じく S.state.profiles から): setOptions =
     顔ぶれが変わらなければ触らない / ドロップダウンを開いている間は組み直さない。
     2.5 秒ポーリングのたびに選択肢を作り直して選択が飛ぶのを防ぐ。 */
  const sel = $("#cb-profile");
  const profs = S.state ? (S.state.profiles || []) : [];
  setOptions(sel, `<option value="">(プロファイルを選択)</option>` + profs.map(p =>
    `<option value="${p.id}">${esc(p.name)} [${esc((backendById(p.backend) || {}).name || p.backend)}]</option>`).join(""),
    "cb-profile", renderCoding, sel.value || (d._restored && d._restored.profile) || "");
  // 保存プロファイルの復元 / 変更があったらモデル名・thinking 選択肢を取得
  if (sel.value && d._optsPid !== sel.value) cbLoadModelOptions();
  const live = sel.value ? liveOf(sel.value) : null;
  const pb = statusBadge(live).replace('class="badge', 'id="cb-prof-state" class="badge');
  if ($("#cb-prof-state")._badge !== pb) {
    $("#cb-prof-state").outerHTML = pb;
    $("#cb-prof-state")._badge = pb;
  }
  // pi / サンドボックスの状態チップ
  const st = d.status;
  if (st) {
    const pi = st.pi || {};
    const installing = pi.install && pi.install.status === "running";
    const piEl = $("#cb-pi-state");
    piEl.className = "badge " + (pi.available ? "ok" : installing ? "warn" : "err");
    piEl.innerHTML = `<span class="dot"></span>` + (pi.available
      ? `pi ${esc(pi.version || "")}` : installing ? "pi インストール中…" : "pi 未インストール");
    if (pi.install && pi.install.status === "failed") piEl.title = pi.install.log || "";
    $("#cb-pi-install").classList.toggle("hidden", pi.available || installing);
    const sb = st.sandbox || {};
    const sbEl = $("#cb-sandbox-state");
    sbEl.className = "badge " + (sb.backend ? "ok" : "warn");
    sbEl.innerHTML = `<span class="dot"></span>` + (sb.backend
      ? `サンドボックス: ${esc(sb.backend)}` : "サンドボックス不可");
    if (sb.hint) sbEl.title = sb.hint;
    $("#cb-env-note").textContent = st.exclusive_owner ? `実走ロック: ${st.exclusive_owner}` : "";
  }
  const a = d.assets;
  $("#cb-skills-note").textContent = a && a.skills
    ? `デフォルトスキル: ${a.skills.map(s => s.name).join(" / ")}` : "";
  $("#cb-start").disabled = d.busy || !!d.active;
  $("#cb-note").textContent = d.active ? "実行中は開始できません (同時実走は 1 件)" : "";
  renderCodingActive(d.runs.find(r => r.id === d.active) || null);
  renderCodingHistory();
  renderCodingViewer();
  renderCodingEval();
  if (d.logOpen && d.active) cbFetchLog().then(() => {
    const pre = $(".cb-log");
    const txt = d.logText || "(まだログがありません)";
    if (pre && pre.textContent !== txt) pre.textContent = txt;   // 変化なしの書き込みは選択を壊す
  });
}