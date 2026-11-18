"""プロファイル編集の「起動 JSON 作成 → JSON 主体モード」切替の回帰テスト。

背景 (2026-11-18 報告): freetoken-fork で起動 JSON 主体から「解除 (全項目
モード)」へ変更した後、再び JSON 主体に戻せない。原因: launchJsonExport /
basaltConfigExport が同一 URL への go() でエディタを開き直そうとするが、
ルーター (syncDialog) は同じ kind+arg のダイアログを開き直さないため、
全項目モードの古い draft (config="") が残る。その状態で「保存」すると
config を空で上書きしてしまい、JSON 紐付けが振り出しに戻る。

修正後は最新プロファイルから draft を組み直す (openEditorDom)。node の
最小スタブで export のフローを実際に走らせ、draft が config 主体へ切り
替わること / その後の保存で config が巻き戻らないことを検証する。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EDITOR_JS = ROOT / "src" / "llm_launcher" / "static" / "js" / "views" / "editor.js"

DRIVER = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[1], "utf8");
const input = JSON.parse(fs.readFileSync(0, "utf8"));

const toasts = [], renders = [], goes = [];
const el = () => ({ style: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
                    addEventListener(){}, querySelectorAll(){ return []; }, hidden: false });
const sandbox = {
  console, JSON, Object, Array, String, Number, Boolean, Math, Promise, Error,
  setTimeout: () => 0, clearTimeout: () => {},
  document: { addEventListener(){}, querySelectorAll(){ return []; } },
  $: sel => (sel === "#modal" || sel === "#modal-veil") ? el() : null,
  toast: m => toasts.push(m),
  can: () => true,
  backendById: bid => input.backend,
  profileById: id => (sandbox.S.state.profiles || []).find(p => p.id === id),
  refresh: async () => {},
  go: (h) => goes.push(h),
  routeUrl: (t, k, a) => `#/${t}/${k}/${a}`,
  S: { tab: "profiles", hf: {}, state: { profiles: [input.profile], backends: [input.backend] },
       editing: JSON.parse(JSON.stringify(input.profile)) },
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "editor.js" });
/* DOM 描画 / 入力収集はスタブ化 (検証対象は export 後の draft 再構築) */
vm.runInContext(`
  renderEditor = () => { renders.push(String((S.editing && S.editing.values || {}).config || "")); };
  collectDraft = () => S.editing;
  schedulePreview = () => {};
`, sandbox);

const profile = input.profile;
sandbox.api = async (url, opts = {}) => {
  const m = opts.method || "GET";
  if (m === "PUT" && url === `/api/profiles/${profile.id}`) {
    Object.assign(profile, JSON.parse(JSON.stringify(opts.body)));
    return profile;
  }
  if (m === "POST" && url === `/api/profiles/${profile.id}/launch-json`) {
    profile.values.config = input.newConfig;
    return { ok: true, path: input.newConfig };
  }
  if (m === "POST" && url === `/api/profiles/${profile.id}/config-json`) {
    profile.values.config = input.newConfig;
    if (!profile.on) profile.on = {};
    profile.on.config = true;
    return { ok: true, path: "/abs" + input.newConfig };
  }
  if (m === "GET" && url === `/api/profiles/${profile.id}/launch-json`)
    return { ok: true, exists: true, path: profile.values.config, launch: { env: {} } };
  throw new Error("unexpected api: " + m + " " + url);
};

(async () => {
  await sandbox[input.fn]();
  const body = sandbox.editorBody();
  console.log(JSON.stringify({
    toasts, renders, goes,
    serverConfig: profile.values.config,
    draftConfig: sandbox.S.editing.values.config,
    saveBodyConfig: body.values.config,
  }));
})().catch(e => { console.error(e); process.exit(1); });
"""

FT_BACKEND = {
    "id": "freetoken-fork", "name": "FreeToken (fork)", "launch_json": True,
    "sections": [{"id": "model", "label": "モデル"}],
    "fields": [
        {"key": "model_path", "flag": "--model-path", "type": "str", "label": "モデル"},
        {"key": "port", "flag": "--port", "type": "str", "label": "ポート"},
        {"key": "api_key", "flag": "--api-key", "type": "str", "label": "APIキー"},
        {"key": "num_tokens", "flag": "--num-tokens", "type": "str", "label": "KVトークン数"},
    ],
}
BASALT_BACKEND = {
    "id": "basalt", "name": "Basalt", "launch_json": False,
    "sections": [{"id": "model", "label": "モデル"}],
    "fields": [
        {"key": "model", "flag": "--model", "type": "str", "label": "モデル"},
        {"key": "config", "flag": "--config", "type": "str", "label": "設定JSON"},
        {"key": "port", "flag": "--port", "type": "str", "label": "ポート"},
    ],
}


def _profile(bid: str) -> dict:
    return {"id": "p1", "name": "ft", "backend": bid,
            "values": {"model_path": "/m", "port": "1919", "num_tokens": "524288",
                       "max_running_requests": "1", "config": ""},
            "on": {"model_path": True, "port": True, "num_tokens": True,
                   "max_running_requests": True},
            "env": {}, "extra_args": "", "cwd": "", "command": "",
            "custom_only": False, "note": ""}


def _run(fn: str, backend: dict, new_config: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    payload = {"fn": fn, "backend": backend, "profile": _profile(backend["id"]),
               "newConfig": new_config}
    out = subprocess.run([node, "-e", DRIVER, str(EDITOR_JS)],
                         input=json.dumps(payload, ensure_ascii=False),
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_launch_json_export_switches_draft_to_json_mode():
    """解除 (config="") の後に JSON を作成したら draft が JSON 主体になる。"""
    r = _run("launchJsonExport", FT_BACKEND, "/mod/fork/freetoken/ft-abc12345.json")
    assert r["serverConfig"] == "/mod/fork/freetoken/ft-abc12345.json"
    assert r["draftConfig"] == "/mod/fork/freetoken/ft-abc12345.json"
    # そのまま「保存」しても config が空へ巻き戻らない (報告された症状の核心)
    assert r["saveBodyConfig"] == "/mod/fork/freetoken/ft-abc12345.json"
    assert any("作成しました" in t for t in r["toasts"])


def test_basalt_config_export_switches_draft_to_json_mode():
    r = _run("basaltConfigExport", BASALT_BACKEND, "data/launch/basalt-p1.json")
    assert r["serverConfig"] == "data/launch/basalt-p1.json"
    assert r["draftConfig"] == "data/launch/basalt-p1.json"
    assert r["saveBodyConfig"] == "data/launch/basalt-p1.json"


def test_export_does_not_rely_on_same_url_go():
    """同一 URL への go() は syncDialog が開き直さない — 再構築は明示的に行う。"""
    r = _run("launchJsonExport", FT_BACKEND, "/mod/ft-x.json")
    assert r["draftConfig"] == "/mod/ft-x.json"   # go() の有無に関わらず切り替わる
