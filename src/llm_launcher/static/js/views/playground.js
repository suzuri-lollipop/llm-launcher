/* ===================================================================
   view: playground — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.playground = `
  <section id="view-playground" class="view hidden">
    <h2 class="view-title">プレイグラウンド <span class="muted">起動中のサーバーとチャット (OpenAI 互換 API 透過プロキシ)</span></h2>
    <div class="card" style="padding:10px 12px">
      <div class="row" style="gap:8px">
        <select id="pg-srv" class="grow" style="min-width:180px" onchange="pgPickSession(this.value)"></select>
        <select id="pg-model" style="max-width:240px" onchange="pgPickModel(this.value)"></select>
        <button class="ghost" onclick="pgLoadModels(true)" title="モデル一覧を再読込"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg></button>
        <span class="grow"></span>
        <button class="ghost" onclick="pgClear()"><svg class=ic aria-hidden=true><use href=#i-trash /></svg> クリア</button>
      </div>
      <details class="adv" id="pg-adv">
        <summary>パラメータ / システムプロンプト</summary>
        <div class="inner">
          <label class="f" for="pg-sys">システムプロンプト</label>
          <textarea id="pg-sys" placeholder="任意: 全ターンに付与される指示"></textarea>
          <div class="row" style="gap:12px;flex-wrap:wrap">
            <div><label class="f" for="pg-temp">temperature</label><input type="number" id="pg-temp" step="0.05" min="0" max="2" placeholder="0.7" style="width:96px"></div>
            <div><label class="f" for="pg-topp">top_p</label><input type="number" id="pg-topp" step="0.01" min="0" max="1" placeholder="0.95" style="width:96px"></div>
            <div><label class="f" for="pg-mtok">max_tokens</label><input type="number" id="pg-mtok" min="1" placeholder="(既定)" style="width:96px"></div>
            <label class="switch" style="align-self:flex-end"><input type="checkbox" id="pg-stream" checked><span class="track"></span><span class="lab" style="font-size:12.5px">ストリーム</span></label>
          </div>
        </div>
      </details>
    </div>
    <div class="card">
      <div id="pg-msgs"></div>
      <div style="margin-top:8px">
        <textarea id="pg-input" placeholder="メッセージを入力… (Enter で送信 / Shift+Enter で改行)" rows="2"></textarea>
        <div class="row" style="margin-top:8px">
          <span class="muted ellip" id="pg-status" style="max-width:55%"></span>
          <span class="grow"></span>
          <button class="ghost" id="pg-stop" onclick="pgStop()" disabled><svg class=ic aria-hidden=true><use href=#i-stop /></svg> Stop</button>
          <button class="primary" id="pg-send" onclick="pgSend()">送信 <svg class=ic aria-hidden=true><use href=#i-send /></svg></button>
        </div>
      </div>
    </div>
  </section>
`;
/* ============================================================= playground */
/* 起動中サーバー (OpenAI 互換 API) とのチャット。リクエストは launcher の
   /api/playground/{sid}/* 透過プロキシ経由 (同一オリジンなので CORS 不要)。
   会話はブラウザの localStorage にサーバー毎 (profile_id 毎) に保存される。 */
const PG_KEY = "llm-laun…d.v1";
function pgInit() {
  let d = {};
  try { d = JSON.parse(localStorage.getItem(PG_KEY) || "{}") || {}; } catch { d = {}; }
  S.pg = {
    sid: d.sid || "", model: d.model || "", convos: d.convos || {},
    models: [], modelsBusy: false, modelsErr: "",
    streaming: false, ctrl: null, _mSig: "", _sSig: "", _modSig: "",
  };
  // 送信中にページを閉じた等で localStorage に残った streaming フラグは実体なしなので片付ける
  for (const list of Object.values(d.convos || {}))
    for (const m of list || []) if (m.streaming) { m.streaming = false; m.aborted = true; delete m._live; }
  const set = (id, v) => { const el = $(id); if (el && v != null && v !== "") el.value = v; };
  set("#pg-sys", d.sys); set("#pg-temp", d.temp); set("#pg-topp", d.topp); set("#pg-mtok", d.mtok);
  if (d.stream === false) $("#pg-stream").checked = false;
}
function pgPersist() {
  if (!S.pg) return;
  const convos = {};
  Object.entries(S.pg.convos).slice(-8).forEach(([k, v]) => convos[k] = (v || []).slice(-60));
  const v = id => $(id) ? $(id).value : "";
  try {
    localStorage.setItem(PG_KEY, JSON.stringify({
      sid: S.pg.sid, model: S.pg.model, convos,
      sys: v("#pg-sys"), temp: v("#pg-temp"), topp: v("#pg-topp"), mtok: v("#pg-mtok"),
      stream: $("#pg-stream") ? $("#pg-stream").checked : true,
    }));
  } catch { /* 保存容量超過などは無視 */ }
}
const pgSession = () => (S.state?.sessions || []).find(s => s.id === S.pg.sid) || null;
const pgPid = () => { const s = pgSession(); return s ? s.profile_id : (S.pg.sid || ""); };
const pgMsgs = () => {
  const pid = pgPid(); if (!pid) return [];
  if (!S.pg.convos[pid]) S.pg.convos[pid] = [];
  return S.pg.convos[pid];
};
function openPlayground(sid) {
  if (!S.pg) pgInit();
  if (S.pg.streaming) return toast("送信中です。停止してから切り替えてください");
  S.pg.sid = sid; S.pg.model = ""; S.pg._modSig = "";
  switchTab("playground");
  pgLoadModels();
}
function renderPlayground() {
  if (!S.state) return;
  if (!S.pg) pgInit();
  const d = S.pg;
  const live = liveSessions();
  if (d.sid && !live.some(s => s.id === d.sid) && !d.streaming) d.sid = "";
  if (!d.sid && live.length && !d.streaming) { d.sid = live[0].id; d.model = ""; pgLoadModels(); }
  const srv = $("#pg-srv");
  const sSig = live.map(s => [s.id, s.profile_name, s.backend_name, s.port, s.ready ? 1 : 0].join(":")).join("|");
  if (sSig !== d._sSig) {
    d._sSig = sSig;
    srv.innerHTML = live.length
      ? live.map(s => `<option value="${s.id}" ${s.id === d.sid ? "selected" : ""}>${esc(s.profile_name)} · ${esc(s.backend_name)} · :${s.port || "-"}${s.ready ? "" : " (起動中)"}</option>`).join("")
      : `<option value="">起動中のサーバーがありません</option>`;
    srv.disabled = !live.length || d.streaming;
  }
  const mod = $("#pg-model");
  const mSig = d.models.join("|") + "#" + d.model;
  if (mSig !== d._modSig) {
    d._modSig = mSig;
    mod.innerHTML = d.models.length
      ? d.models.map(m => `<option ${m === d.model ? "selected" : ""} value="${esc(m)}">${esc(m)}</option>`).join("")
      : `<option value="">${d.modelsBusy ? "モデル読み込み中…" : (d.modelsErr ? "モデル取得失敗" : "モデル指定なし")}</option>`;
  }
  mod.disabled = d.streaming || !d.models.length;
  const s = pgSession();
  updatePgStatus();
  if (d.modelsErr) $("#pg-status").innerHTML += ` <span class="pg-err">· ${esc(d.modelsErr)}</span>`;
  $("#pg-send").disabled = d.streaming || !s;
  $("#pg-stop").disabled = !d.streaming;
  $("#pg-input").disabled = !s;
  pgRenderMsgs();
}
function pgPickSession(sid) {
  if (S.pg.streaming) { toast("送信中です"); renderPlayground(); return; }
  S.pg.sid = sid; S.pg.model = ""; S.pg._modSig = "";
  pgLoadModels(); pgPersist();
}
function pgPickModel(m) { S.pg.model = m; pgPersist(); }
async function pgLoadModels() {
  const d = S.pg;
  if (!d.sid) { d.models = []; d.modelsErr = ""; renderPlayground(); return; }
  if (d.modelsBusy) return;
  d.modelsBusy = true; d.modelsErr = ""; renderPlayground();
  try {
    const j = await api(`/api/playground/${d.sid}/models`);
    d.models = (j.data || []).map(m => m.id || m.model).filter(Boolean);
    if (!d.models.includes(d.model)) d.model = d.models[0] || "";
  } catch (e) {
    d.models = []; d.model = ""; d.modelsErr = e.message;
  } finally {
    d.modelsBusy = false; pgPersist(); renderPlayground();
  }
}
/* ---- 軽量レンダラ: コードフェンス / インラインコード / 太字 のみ ---- */
function pgMd(src) {
  const parts = String(src).split("```");
  return parts.map((p, i) => {
    if (i % 2 === 1) {
      let lang = "", code = p;
      const nl = p.indexOf("\n");
      if (nl >= 0 && nl < 30) { lang = p.slice(0, nl).trim(); code = p.slice(nl + 1); }
      if (code.endsWith("\n")) code = code.slice(0, -1);
      return `<pre class="pg-pre"><div class="pg-pre-hd"><span>${esc(lang || "code")}</span>`
           + `<button class="ghost" style="min-height:0;padding:1px 8px;font-size:11px" onclick="pgCopyCode(this)">コピー</button></div>`
           + `<code>${esc(code)}</code></pre>`;
    }
    return esc(p)
      .replace(/`([^`\n]+)`/g, '<code class="pg-ic">$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
      .replace(/\n/g, "<br>");
  }).join("");
}
function pgCopyCode(btn) {
  const code = btn.closest("pre").querySelector("code");
  (navigator.clipboard?.writeText(code.textContent) ?? Promise.reject())
    .then(() => toast("コードをコピーしました")).catch(() => toast("コピー権限がありません"));
}
/* ---- トークン推定 / 生成速度 ---- */
/* API が usage を返さないサーバー向けに内容からトークン数を推定する。
   CJK 等 ≈0.6 tok/字、その他の語 ≈1 tok/語。表示用の近似値 (<svg class=ic aria-hidden=true><use href=#i-bolt /></svg> の数値は usage 優先)。
   配信中の累計は小数を足す estTokFrac を使う。多くのサーバーは 1 token を 1 delta
   で送るため、delta 毎に Math.round/max(1,..) すると端数が毎回切り上げられ、
   CJK 主体の文章で速度が過大になる (実測: 日本語で +12%、瞬間値で 1.4 倍)。 */
function estTokFrac(s) {
  const str = String(s || "");
  if (!str) return 0;
  const cjkRe = /[\u1100-\u115f\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]/g;
  const cjk = str.match(cjkRe) || [];
  const rest = str.replace(cjkRe, " ");
  return cjk.length * 0.6 + (rest.match(/[A-Za-z0-9_'-]+/g) || []).length;
}
function estTok(s) {
  if (!String(s || "")) return 0;
  return Math.max(1, Math.round(estTokFrac(s)));
}
function pgMsgHtml(m, i) {
  const me = m.role === "user";
  const t = m.ts ? new Date(m.ts * 1000).toLocaleTimeString("ja-JP", {hour: "2-digit", minute: "2-digit"}) : "";
  let src = m.content || "";
  if (m.streaming && (src.match(/```/g) || []).length % 2) src += "\n```";   // 配信中の未閉フェンス対策
  const body = m.error
    ? `<span class="pg-err"><svg class=ic aria-hidden=true><use href=#i-alert /></svg> ${esc(m.error)}</span>` + (src ? pgMd(src) : "")
    : (src ? pgMd(src) : (m.streaming ? `<span class="muted">考え中…</span>` : "<i class=\"muted\">(空)</i>"));
  const reason = m.reasoning
    ? `<details class="pg-reason"><summary><svg class=ic aria-hidden=true><use href=#i-chat /></svg> 推論 (${m.reasoning.length.toLocaleString()} 字)</summary><div class="rb">${esc(m.reasoning)}</div></details>` : "";
  // bits は <svg> を含む HTML 断片。必ず esc せず join すること
  // (esc するとアイコンが文字列としてベタ表示されていた)。格納値は数値・定数のみ。
  const bits = [];
  if (!me && m.streaming && m._live) {
    // 配信中: 生成速度をリアルタイム表示
    const lv = m._live, now = performance.now();
    if (lv.tFirst == null) {
      bits.push(`<svg class=ic aria-hidden=true><use href=#i-hourglass /></svg> first token 待ち… ${((now - lv.t0) / 1000).toFixed(1)}s`);
    } else {
      if (lv.tps != null) bits.push(`<svg class=ic aria-hidden=true><use href=#i-bolt /></svg> ${lv.tps.toFixed(1)} tok/s`);
      if (lv.n > 0) bits.push(`${lv.n} tok${lv.est ? " ≈" : ""}`);
      bits.push(`gen ${((now - lv.tFirst) / 1000).toFixed(1)}s`);
    }
  } else if (!me && m.stats) {
    // 終了後: 総所要 + TTFT + トークン数 + 生成速度 (TTFT 除いた生成時間ベース)
    bits.push(m.stats.secs.toFixed(1) + "s");
    if (m.stats.ttft != null) bits.push("first " + m.stats.ttft.toFixed(2) + "s");
    if (m.usage && m.usage.completion_tokens != null) {
      // esc を外すためエンジン由来の値も数値に正規化 (文字列が混入してもタグ化しない)
      const ut = Number(m.usage.completion_tokens), upt = Number(m.usage.prompt_tokens);
      bits.push((isFinite(upt) ? upt + "→" : "") + (isFinite(ut) ? ut : m.usage.completion_tokens) + " tok");
    } else if (m.stats.tok != null) {
      bits.push(`≈${m.stats.tok} tok`);
    } else {
      bits.push(`${(m.content || "").length.toLocaleString()} 字`);
    }
    if (m.stats.tps != null) bits.push(`<svg class=ic aria-hidden=true><use href=#i-bolt /></svg> ${m.stats.tps.toFixed(1)} tok/s`);
  }
  if (m.aborted) bits.push("中断");
  return `<div class="pg-msg ${me ? "me" : "ai"}">
    <div class="who">${esc(me ? "あなた" : (pgSession()?.backend_name || "model"))}<span style="font-weight:400">${t}</span></div>
    ${reason}<div class="pg-body${m.streaming ? " pg-cursor" : ""}">${body}</div>
    ${bits.length ? `<div class="pg-meta">${bits.join(" · ")}`
        + (!me ? `<button class="ghost" style="min-height:0;padding:0 8px;font-size:11px" onclick="pgCopyMsg(${i})">コピー</button>` : "")
        + `</div>` : ""}
  </div>`;
}
function pgRenderMsgs(force) {
  const el = $("#pg-msgs"); if (!el) return;
  const msgs = pgMsgs();
  const last = msgs[msgs.length - 1] || {};
  const sig = [pgPid(), msgs.length, (last.content || "").length + "/" + (last.reasoning || "").length,
               last.streaming ? "s" : "", last.error || ""].join("~");
  if (!force && sig === S.pg._mSig) return;
  S.pg._mSig = sig;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 140;
  if (!msgs.length) {
    el.innerHTML = `<div class="empty">まだ会話がありません。メッセージを送ると、このサーバーに直接話しかけられます。<br>
      <span class="muted">会話はこのブラウザ (サーバー毎) に保存されます。</span></div>`;
  } else {
    el.innerHTML = msgs.map((m, i) => pgMsgHtml(m, i)).join("");
  }
  if (nearBottom) el.scrollTop = el.scrollHeight;
  if (S.pg && S.pg.streaming) updatePgStatus();
}
/* ステータスバー: 配信中は生成速度をリアルタイム表示、それ以外はサーバー状態 */
function updatePgStatus() {
  const st = $("#pg-status"); if (!st || !S.pg) return;
  const last = (pgMsgs().slice(-1)[0] || {});
  const lv = last._live;
  let html;
  if (S.pg.streaming) {
    if (lv && lv.tFirst != null && lv.tps != null)
      html = `<svg class=ic aria-hidden=true><use href=#i-bolt /></svg> ${lv.tps.toFixed(1)} tok/s · ${lv.n} tok${lv.est ? " ≈" : ""} · ${((performance.now() - lv.t0) / 1000).toFixed(1)}s`;
    else if (lv) html = `<svg class=ic aria-hidden=true><use href=#i-hourglass /></svg> first token 待ち… ${((performance.now() - lv.t0) / 1000).toFixed(1)}s`;
    else html = "送信中…";
  } else {
    const s = pgSession();
    html = !s ? `<span class="pg-err">チャットできるサーバーがありません。「起動」タブでサーバーをスタートしてください</span>`
              : (s.ready ? `稼働中 · :${s.port}`
                         : `<span style="color:var(--warn)">起動中… 応答するまでもう少しかかるかも</span>`);
  }
  st.innerHTML = html;
}
let pgRaf = 0;
function pgScheduleRender() {
  if (pgRaf) return;
  pgRaf = requestAnimationFrame(() => { pgRaf = 0; pgRenderMsgs(); });
}
async function pgCopyMsg(i) {
  const txt = (pgMsgs()[i] || {}).content || "";
  if (!txt) return;
  try { await navigator.clipboard.writeText(txt); toast("コピーしました"); }
  catch { toast("コピー権限がありません"); }
}
async function pgSend() {
  const d = S.pg; if (!d || d.streaming) return;
  const inp = $("#pg-input"); const text = (inp.value || "").trim();
  if (!d.sid) return toast("サーバーを選択してください");
  if (!text) return;
  const msgs = pgMsgs();
  const history = [];
  const sys = $("#pg-sys").value.trim(); if (sys) history.push({role: "system", content: sys});
  for (const m of msgs) if (m.content) history.push({role: m.role, content: m.content});
  history.push({role: "user", content: text});
  msgs.push({role: "user", content: text, ts: Date.now() / 1000});
  const asst = {role: "assistant", content: "", reasoning: "", ts: Date.now() / 1000, streaming: true};
  msgs.push(asst);
  inp.value = "";
  d.streaming = true; d.ctrl = new AbortController();
  renderPlayground(); pgPersist();
  const body = {messages: history, stream: $("#pg-stream").checked};
  if (d.model) body.model = d.model;
  const num = id => { const v = $(id).value.trim(); return v === "" ? null : parseFloat(v); };
  const temp = num("#pg-temp"), topp = num("#pg-topp"), mtok = num("#pg-mtok");
  if (temp != null && !isNaN(temp)) body.temperature = temp;
  if (topp != null && !isNaN(topp)) body.top_p = topp;
  if (mtok != null && !isNaN(mtok)) body.max_tokens = Math.floor(mtok);
  const t0 = performance.now(); let tFirst = null;
  // リアルタイム生成速度の追跡 (lv.n = 累積トークン数, lv.frac = 推定時の小数累積, lv.win = 直近4秒のサンプル)
  const lv = {t0, tFirst: null, n: 0, frac: 0, est: true, win: [], tps: null};
  asst._live = lv;
  try {
    const r = await fetch(`/api/playground/${d.sid}/chat`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body), signal: d.ctrl.signal,
    });
    if (!r.ok) {
      let detail = "HTTP " + r.status;
      try { const j = await r.json(); if (j && (j.detail || j.error)) detail = j.detail || JSON.stringify(j.error); } catch { /* ignore */ }
      throw new Error(String(detail).slice(0, 400));
    }
    if (body.stream) {
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        if (tFirst === null && value && value.length) tFirst = performance.now();
        buf += dec.decode(value, {stream: true});
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (!data || data === "[DONE]") continue;
          let evt; try { evt = JSON.parse(data); } catch { continue; }
          if (evt.usage) {
            asst.usage = evt.usage;
            const ut = evt.usage.completion_tokens;
            if (ut != null) { lv.n = Math.max(lv.n, ut); lv.est = false; }   // usage 到着後は実数に切替
          }
          const ch = (evt.choices || [])[0];
          const dl = ch && ch.delta || {};
          const re = dl.reasoning_content || dl.reasoning;   // vLLM: reasoning / SGLang・llama.cpp: reasoning_content
          let grew = false;
          if (re) { asst.reasoning += re; lv.frac += estTokFrac(re); grew = true; }
          if (dl.content) { asst.content += dl.content; lv.frac += estTokFrac(dl.content); grew = true; }
          if (grew) {
            if (lv.est) lv.n = Math.round(lv.frac);   // 小数累積を一度だけ丸める (delta 毎の丸めは過大)
            const now = performance.now();
            if (lv.tFirst == null) lv.tFirst = now;
            lv.win.push({t: now, n: lv.n});
            while (lv.win.length > 2 && now - lv.win[0].t > 4000) lv.win.shift();
            const genS = (now - lv.tFirst) / 1000;
            const s0 = lv.win[0];
            const winS = (now - s0.t) / 1000;
            const oTps = genS > 0.3 ? lv.n / genS : null;
            lv.tps = winS >= 1.2 ? (lv.n - s0.n) / winS : oTps;   // 1.2秒以上は滑動窓平均、それ前は全体平均
            pgScheduleRender();
          }
        }
      }
    } else {
      const j = await r.json();
      const ch = (j.choices || [])[0] || {};
      asst.content = (ch.message && ch.message.content) || "";
      asst.reasoning = (ch.message && (ch.message.reasoning_content || ch.message.reasoning)) || "";
      if (j.usage) asst.usage = j.usage;
      if (!asst.content && ch.error) asst.error = JSON.stringify(ch.error);
    }
  } catch (e) {
    if (e.name === "AbortError") asst.aborted = true;
    else { asst.error = e.message; toast("送信失敗: " + e.message); }
  } finally {
    asst.streaming = false;
    const secs = (performance.now() - t0) / 1000;
    // トークン数: API の usage が最優先、無い場合は推定値
    let tok = null;
    if (asst.usage && asst.usage.completion_tokens != null) tok = asst.usage.completion_tokens;
    else if (lv.n > 0) tok = lv.n;
    else if (asst.content || asst.reasoning) tok = estTok(asst.content + asst.reasoning);
    // 生成速度: 最初のトークン到着から (TTFT を除く) の生成時間ベース
    const genS = lv.tFirst != null ? (performance.now() - lv.tFirst) / 1000 : null;
    let tps = null;
    if (tok != null) {
      const denom = (genS != null && genS > 0.2) ? genS : (secs > 0.2 ? secs : null);
      if (denom != null) tps = tok / denom;
    }
    asst.stats = {secs, ttft: tFirst != null ? (tFirst - t0) / 1000 : null,
                  tok, est: tok != null && !asst.usage, tps};
    delete asst._live;
    d.streaming = false; d.ctrl = null;
    pgPersist(); renderPlayground();
  }
}
function pgStop() { if (S.pg && S.pg.ctrl) S.pg.ctrl.abort(); }
function pgClear() {
  const msgs = pgMsgs();
  if (!msgs.length) return toast("会話は空です");
  if (S.pg.streaming) return toast("送信中はクリアできません");
  if (!confirm("このサーバーとの会話を削除しますか?")) return;
  S.pg.convos[pgPid()] = []; pgPersist(); renderPlayground();
}

