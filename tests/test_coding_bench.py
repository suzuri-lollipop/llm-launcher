"""コーディングベンチマーク (coding.CodingManager / pi ハーネス / サンドボックス /
2 実装ビューアー) の回帰テスト。

押さえる点:
  1. デフォルト資材 (スキル群 / AGENTS.md / プロンプト) の生成と冪等性
  2. bubblewrap サンドボックスの argv 構築 (rw は run_dir だけ / symlink 再現)
  3. pi への受け渡し: models.json (launcher のサーバーを OpenAI 互換プロバイダ化) /
     AGENTS.md コピー / --skill 列挙 / backend → frontend のセッション継続
  4. 偽 pi (JSONL イベントを出すスクリプト) での実行フロー・進捗・トークン積算
  5. 中断 / タイムアウト / pi 失敗の扱いと実走排他 (ベンチと同じロック)
  6. ファイル参照のトラバーサル拒否 / プレビュー検出 (app / static) / 静的配信
  7. API  validation (404 / 422 / 409) と履歴削除・一括クリア・lost 回復
  8. フロントエンド契約 (ビューアー / メニュー / ルーティング)
"""
from __future__ import annotations

import asyncio
import json
import stat
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from llm_launcher import coding as coding_mod
from llm_launcher import rbac
from llm_launcher.bench import ExclusiveLock
from llm_launcher.coding import CodingAssets, CodingError, CodingManager, PiTool
from llm_launcher.store import JsonStore

from test_auth_users import Client


# ------------------------------------------------------------------ 偽サーバー / 偽 pi


class _ModelHandler(BaseHTTPRequestHandler):
    """health と /v1/models だけ返す最小の OpenAI 互換サーバー。"""

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            body = b'{"ok":true}'
        elif self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "test-model"}]}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, srv.server_address[1]
    srv.shutdown()


class _EffortModelHandler(_ModelHandler):
    """/v1/models が Inkling-2 相当の effort 語彙 (none 含む) を公開するサーバー。"""

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/models":
            body = json.dumps({"data": [{
                "id": "test-model",
                "supported_reasoning_efforts":
                    ["max", "xhigh", "high", "medium", "low", "minimal", "none"],
                "default_reasoning_effort": "high",
            }]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


@pytest.fixture
def effort_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _EffortModelHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, srv.server_address[1]
    srv.shutdown()


FAKE_PI = r'''#!/usr/bin/env python3
"""偽 pi: --version / --mode json のふりをして JSONL イベントを出し、ファイルを書く。"""
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
if "--version" in argv:
    print("fake-pi 1.0.0")
    sys.exit(0)
prompt = argv[argv.index("--") + 1] if "--" in argv else ""
if "FAIL_PI" in prompt:
    print("boom: provider unreachable", file=sys.stderr)
    sys.exit(1)
# pi-home の models.json が launcher のサーバーを指していることを検証
pi_dir = os.environ.get("PI_CODING_AGENT_DIR", "")
cfg = json.loads((Path(pi_dir) / "models.json").read_text())
prov = cfg["providers"]["launcher"]
assert prov["baseUrl"].startswith("http://127.0.0.1:")
assert prov["models"][0]["id"] == "test-model"
assert Path(os.environ["PI_CODING_AGENT_SESSION_DIR"]).is_dir()
assert (Path.cwd() / "AGENTS.md").is_file()
slow = "SLOW_PI" in prompt

def ev(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)

ev({"type": "session", "version": 3, "id": "s", "cwd": str(Path.cwd())})
ev({"type": "agent_start"})
ev({"type": "turn_start"})
ev({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "write",
    "args": {"path": "out.txt"}})
if slow:
    import time
    for _ in range(300):
        time.sleep(0.2)
nofiles = "NOFILES" in prompt
backend = "バックエンド" in prompt
if not nofiles:
    if backend:
        Path("backend").mkdir(exist_ok=True)
        Path("backend/server.py").write_text(
            "import json, os\n"
            "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({'ok': True, 'path': self.path}).encode()\n"
            "        self.send_response(200 if self.path == '/api/health' else 404)\n"
            "        self.send_header('Content-Type', 'application/json')\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "    def log_message(self, *a):\n"
            "        pass\n"
            "ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PORT', '8000'))), H)"
            ".serve_forever()\n", encoding="utf-8")
    else:
        Path("frontend").mkdir(exist_ok=True)
        Path("frontend/index.html").write_text(
            "<!doctype html><title>tasks</title><h1>Tasks</h1>", encoding="utf-8")
        Path("frontend/app.js").write_text("fetch('api/health')\n", encoding="utf-8")
ev({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "write",
    "result": {"content": [{"type": "text",
                            "text": "Validation failed" if nofiles else "ok"}]},
    "isError": nofiles})
ev({"type": "message_start", "message": {"role": "assistant", "content": []}})
ev({"type": "message_update", "usage": {"input": 50, "output": 10},
    "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "作成"}})
ev({"type": "message_end", "message": {"role": "assistant",
    "stopReason": "length" if nofiles else "stop",
    "usage": {"input": 100, "output": 20},
    "content": [{"type": "text", "text": ("バックエンド" if backend else "フロントエンド")
                  + "を作成しました"}]}})
ev({"type": "turn_end", "message": {"role": "assistant"}, "toolResults": []})
ev({"type": "agent_end", "messages": [], "willRetry": False})
ev({"type": "agent_settled", "aborted": False})
'''


class _FakeManager:
    """ProcessManager の代わり (実プロセスは起動しない)。"""

    def __init__(self, port: int):
        self.sessions: dict[str, dict] = {}
        self.stopped: list[str] = []
        self.port = port

    def _session(self, sid: str, **kw) -> dict:
        s = {"id": sid, "profile_id": "p1", "status": "running", "port": self.port,
             "health_path": "/health", "argv": [], **kw}
        self.sessions[sid] = s
        return s

    async def start(self, profile, *, force=False, actor=None, via=""):
        return self._session("s_new")

    async def stop(self, sid):
        self.stopped.append(sid)
        if sid in self.sessions:
            self.sessions[sid]["status"] = "exited"


PROFILE = {"id": "p1", "name": "テストプロファイル", "backend": "vllm"}


def _write_fake_pi(tmp_path: Path) -> str:
    p = tmp_path / "fake-pi.py"
    p.write_text(FAKE_PI, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _manager(tmp_path: Path, fake: _FakeManager, pi_cmd: str = "") -> CodingManager:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    assets = CodingAssets(tmp_path)
    assets.ensure_defaults()
    settings = JsonStore(tmp_path / "data" / "settings.json", {})
    if pi_cmd:
        settings.mutate(lambda d: d.update({"coding": {"pi_command": pi_cmd}}))
    pi = PiTool(settings, tmp_path / "data" / "coding" / "pi-install.log")
    store = JsonStore(tmp_path / "data" / "coding.json", [])
    profiles = JsonStore(tmp_path / "data" / "profiles.json", [])
    profiles.mutate(lambda lst: lst.append(PROFILE))
    mgr = CodingManager(tmp_path, store, profiles, fake, assets, pi)
    mgr.attach_exclusive(ExclusiveLock())
    return mgr


def _params(mgr: CodingManager, **kw) -> dict:
    d = mgr.assets.defaults_info()
    p = {"model": "", "name": "テスト実行", "skills_dir": d["skills_dir"],
         "agents_path": d["agents_path"],
         "backend_prompt": "タスク管理のバックエンドを作ってください",
         "frontend_prompt": "タスク管理のフロントエンドを作ってください",
         "thinking": "off", "phase_timeout": 60, "ready_timeout": 30,
         "auto_start": False, "stop_after": False, "sandbox": False}
    p.update(kw)
    return p


def _run_to_finish(mgr: CodingManager, params: dict, seed_session: bool = True) -> dict:
    async def go() -> dict:
        fake = mgr.manager
        if seed_session:
            fake._session("s1")
        run = await mgr.start(PROFILE, params, "vLLM")
        await mgr._tasks[run["id"]]
        return mgr.get(run["id"])
    return asyncio.run(go())


# ------------------------------------------------------------------ 1. デフォルト資材


def test_defaults_created_and_idempotent(tmp_path):
    assets = CodingAssets(tmp_path)
    assets.ensure_defaults()
    info = assets.defaults_info()
    assert Path(info["agents_path"]).is_file()
    assert Path(info["skills_dir"]).is_dir()
    names = {s["name"] for s in info["skills"]}
    assert {"python-stdlib-backend", "vanilla-frontend", "self-check"} <= names
    assert all(s["description"] for s in info["skills"])
    assert "backend/server.py" in info["backend_prompt"]
    assert "frontend/" in info["frontend_prompt"]
    # ユーザー編集は上書きしない
    Path(info["agents_path"]).write_text("custom rules", encoding="utf-8")
    (Path(info["skills_dir"]) / "python-stdlib-backend" / "SKILL.md").write_text(
        "---\nname: python-stdlib-backend\ndescription: edited\n---\n", encoding="utf-8")
    assets.ensure_defaults()
    assert Path(info["agents_path"]).read_text(encoding="utf-8") == "custom rules"
    edited = [s for s in assets.list_skills() if s["name"] == "python-stdlib-backend"]
    assert edited[0]["description"] == "edited"


def test_skill_dirs_enumeration(tmp_path):
    assets = CodingAssets(tmp_path)
    assets.ensure_defaults()
    mgr = _manager(tmp_path, _FakeManager(0))
    dirs = mgr._skill_dirs(assets.skills_dir)
    assert len(dirs) == 3 and all((d / "SKILL.md").is_file() for d in dirs)
    # フォルダ自身がスキルの場合
    one = assets.skills_dir / "self-check"
    assert mgr._skill_dirs(one) == [one]


def test_pi_command_standalone_installer_layout(tmp_path, monkeypatch):
    """pi.dev の install.sh は ~/.local/bin/pi/ (ディレクトリ) に一式を置く。
    which では見つからないので中の pi 本体を解決する。"""
    mgr = _manager(tmp_path, _FakeManager(0))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(coding_mod.shutil, "which", lambda name: None)
    bindir = tmp_path / ".local" / "bin" / "pi"
    bindir.mkdir(parents=True)
    exe = bindir / "pi"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert mgr.pi.command() == str(exe)
    assert mgr.pi.available() is True


# ------------------------------------------------------------------ 2. サンドボックス


def test_sandbox_argv_binds(tmp_path):
    rd = tmp_path / "run"
    ws = rd / "workspace"
    ws.mkdir(parents=True)
    skills = tmp_path / "skills"
    skills.mkdir()
    argv = coding_mod.sandbox_argv(rd, ws, ro_binds=[skills])
    assert argv[0] == "bwrap"
    for flag in ("--unshare-pid", "--die-with-parent", "--new-session", "--cap-drop"):
        assert flag in argv
    i = argv.index("--bind")
    assert argv[i + 1] == str(rd) and argv[i + 2] == str(rd)   # rw は run_dir だけ
    j = argv.index(str(skills))
    assert argv[j - 1] == "--ro-bind"
    assert argv[-2:] == ["--chdir", str(ws)]
    # ネットワーク名前空間は分離しない (モデルサーバーへ届くため)
    assert "--unshare-net" not in argv


def test_bind_args_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert coding_mod._bind_args(link, link, ro=True) == ["--symlink", str(target), str(link)]
    assert coding_mod._bind_args(tmp_path / "missing", tmp_path / "missing", ro=True) == []
    assert coding_mod._bind_args(target, target, ro=True) == ["--ro-bind", str(target), str(target)]


def test_sandbox_env_isolation(tmp_path):
    rd = tmp_path / "run"
    env = coding_mod.sandbox_env(rd / "home", rd / "pi-home", rd / "sessions")
    assert env["HOME"] == str(rd / "home")
    assert env["PI_CODING_AGENT_DIR"] == str(rd / "pi-home")
    assert env["PI_OFFLINE"] == "1" and env["PI_TELEMETRY"] == "0"
    assert "PATH" in env


# ------------------------------------------------------------------ 3/4. 実行フロー (偽 pi)


def test_full_run_backend_then_frontend(tmp_path, server):
    _, port = server
    pi_cmd = _write_fake_pi(tmp_path)
    mgr = _manager(tmp_path, _FakeManager(port), pi_cmd)
    run = _run_to_finish(mgr, _params(mgr))
    assert run["status"] == "completed", run["error"]
    assert run["model"] == "test-model"
    ws = mgr.run_dir(run["id"]) / "workspace"
    assert (ws / "backend" / "server.py").is_file()
    assert (ws / "frontend" / "index.html").is_file()
    assert (ws / "AGENTS.md").is_file()
    assert run["files"]["count"] >= 4
    # フェーズ統計: トークンは message_end の積算 (2 フェーズそれぞれ in 100 / out 20)
    for phase in ("backend", "frontend"):
        st = run["phases"][phase]
        assert st["status"] == "completed" and st["exit_code"] == 0
        assert st["turns"] == 1 and st["tool_calls"] == 1
        assert st["tokens_in"] == 100 and st["tokens_out"] == 20
        # ターン別時間計測 (生成時間 / TTFT / ターン時間 / デコード速度)
        assert st["gen_n"] == 1 and st["gen_secs_avg"] is not None
        assert st["ttft_n"] == 1 and st["ttft_avg"] is not None
        assert st["turn_n"] == 1 and st["turn_secs_avg"] is not None
        assert st["decode_tps"] is not None and st["decode_tps"] > 0
        assert "_msg_t0" not in st and "_turn_t0" not in st   # 一時キーは畳まれている
    # pi-home の models.json がサーバーを指す
    cfg = json.loads((mgr.run_dir(run["id"]) / "pi-home" / "models.json").read_text())
    prov = cfg["providers"]["launcher"]
    assert prov["baseUrl"] == f"http://127.0.0.1:{port}/v1"
    assert prov["api"] == "openai-completions"
    assert prov["models"][0]["id"] == "test-model"
    # セッション継続: 同じ session-id で 2 フェーズ (sessions ディレクトリに記録)
    assert (mgr.run_dir(run["id"]) / "sessions").is_dir()
    # ダイジェストログにツールとアシスタント文が残る
    log = (mgr.run_dir(run["id"]) / "logs" / "backend.log").read_text(encoding="utf-8")
    assert "[tool] write" in log and "バックエンドを作成しました" in log


def test_run_auto_start_and_stop_after(tmp_path, server):
    _, port = server
    pi_cmd = _write_fake_pi(tmp_path)
    fake = _FakeManager(port)
    mgr = _manager(tmp_path, fake, pi_cmd)
    run = _run_to_finish(mgr, _params(mgr, auto_start=True, stop_after=True),
                         seed_session=False)
    assert run["status"] == "completed"
    assert run["auto_started"] is True and run["stopped_after"] is True
    assert fake.stopped == ["s_new"]


def test_exclusive_lock_shared_with_bench(tmp_path, server):
    _, port = server
    mgr = _manager(tmp_path, _FakeManager(port), _write_fake_pi(tmp_path))
    assert mgr.exclusive.acquire("bench:p1") is True
    with pytest.raises(CodingError, match="実走できません"):
        asyncio.run(mgr.start(PROFILE, _params(mgr), "vLLM"))
    mgr.exclusive.release()


def test_pi_missing_is_user_error(tmp_path):
    mgr = _manager(tmp_path, _FakeManager(0))     # pi_command 未設定・pi 未導入
    mgr.pi._version_cache = None
    mgr.pi.command = lambda: ""
    with pytest.raises(CodingError, match="pi が見つかりません"):
        asyncio.run(mgr.start(PROFILE, _params(mgr), "vLLM"))
    assert mgr.exclusive.owner == ""              # ロックは解放済み


def test_sandbox_required_when_enabled(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, _FakeManager(0), _write_fake_pi(tmp_path))
    monkeypatch.setattr(coding_mod, "sandbox_backend", lambda settings=None: "")
    monkeypatch.setattr(coding_mod, "sandbox_info",
                        lambda settings=None: {"hint": "sudo apt install bubblewrap"})
    with pytest.raises(CodingError, match="サンドボックス"):
        asyncio.run(mgr.start(PROFILE, _params(mgr, sandbox=True), "vLLM"))
    assert mgr.exclusive.owner == ""


def test_docker_argv_and_pi_mounts(tmp_path):
    rd = tmp_path / "run"
    ws = rd / "workspace"
    ws.mkdir(parents=True)
    skills = tmp_path / "skills"
    skills.mkdir()
    # standalone インストーラ配置 (package.json を持つ pi/ ディレクトリ) は丸ごと ro
    pi_dir = tmp_path / ".local" / "bin" / "pi"
    pi_dir.mkdir(parents=True)
    (pi_dir / "package.json").write_text("{}")
    exe = pi_dir / "pi"
    exe.write_text("x")
    mounts = coding_mod.pi_runtime_mounts(str(exe))
    assert mounts == [pi_dir]
    argv = coding_mod.docker_argv(rd, ws, ro_binds=[skills],
                                  env={"HOME": str(rd / "home")},
                                  image="python:3.12-slim", name="llmcb-x-backend",
                                  pi_mounts=mounts)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv and "host" in argv
    assert f"{rd}:{rd}" in argv and f"{skills}:{skills}:ro" in argv
    assert f"{pi_dir}:{pi_dir}:ro" in argv
    assert "HOME=" + str(rd / "home") in argv
    assert argv[-1] == "python:3.12-slim"
    assert "--init" in argv and "--user" in argv


def test_pi_failure_marks_run_failed(tmp_path, server):
    _, port = server
    mgr = _manager(tmp_path, _FakeManager(port), _write_fake_pi(tmp_path))
    run = _run_to_finish(mgr, _params(mgr, backend_prompt="FAIL_PI バックエンドを作ってください"))
    assert run["status"] == "failed"
    assert "exit 1" in run["error"] and "boom" in run["error"]
    assert run["phases"]["backend"]["status"] == "failed"


def test_cancel_kills_pi(tmp_path, server):
    _, port = server
    mgr = _manager(tmp_path, _FakeManager(port), _write_fake_pi(tmp_path))

    async def go() -> dict:
        mgr.manager._session("s1")
        run = await mgr.start(PROFILE, _params(mgr, backend_prompt="SLOW_PI バックエンド"), "vLLM")
        await asyncio.sleep(0.8)
        assert mgr.get(run["id"])["status"] == "running"   # 実行中バッジ (待機ではない)
        mgr.cancel(run["id"])
        await asyncio.wait_for(mgr._tasks[run["id"]], timeout=15)
        return mgr.get(run["id"])
    run = asyncio.run(go())
    assert run["status"] == "cancelled"
    assert mgr.exclusive.owner == ""


def test_phase_timeout(tmp_path, server):
    _, port = server
    mgr = _manager(tmp_path, _FakeManager(port), _write_fake_pi(tmp_path))
    run = _run_to_finish(mgr, _params(mgr, phase_timeout=1,
                                      backend_prompt="SLOW_PI バックエンド"))
    assert run["status"] == "failed"
    assert "タイムアウト" in run["error"]


def test_no_files_run_reports_warning(tmp_path, server):
    """モデルがツールを使えずファイルを書かなかった場合、完了でも正直に注記する。"""
    _, port = server
    mgr = _manager(tmp_path, _FakeManager(port), _write_fake_pi(tmp_path))
    run = _run_to_finish(mgr, _params(mgr, backend_prompt="NOFILES バックエンド",
                                      frontend_prompt="NOFILES フロントエンド"))
    assert run["status"] == "completed"
    assert run["files"]["count"] == 1                     # AGENTS.md だけ
    assert "実装ファイルが生成されませんでした" in run["warning"]
    assert "検証エラー 2 回" in run["warning"]
    assert "打ち切られた" in run["warning"]
    for ph in ("backend", "frontend"):
        assert run["phases"][ph]["tool_errors"] == 1
        assert run["phases"][ph]["stop_reason"] == "length"
    log = (mgr.run_dir(run["id"]) / "logs" / "backend.log").read_text(encoding="utf-8")
    assert "[tool-error]" in log


def test_consume_event_token_accumulation(tmp_path):
    mgr = _manager(tmp_path, _FakeManager(0))
    run = {"id": "cb_x", "progress": {"turns": 0, "tool_calls": 0, "tokens_in": 0,
                                     "tokens_out": 0, "text": "", "last_tool": ""}}
    stats = {"turns": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0}
    import io
    digest = io.StringIO()
    for i in range(2):
        mgr._consume_event(run, "backend", {"type": "turn_start"}, digest, stats)
        mgr._consume_event(run, "backend", {"type": "tool_execution_start",
                                            "toolName": "bash", "args": {"command": "ls"}},
                           digest, stats)
        mgr._consume_event(run, "backend", {"type": "message_end", "message": {
            "role": "assistant", "usage": {"input": 100, "output": 20},
            "content": [{"type": "text", "text": f"done {i}"}]}}, digest, stats)
    assert stats["tokens_in"] == 200 and stats["tokens_out"] == 40
    assert stats["turns"] == 2 and stats["tool_calls"] == 2
    assert run["progress"]["text"] == "done 1"
    assert "[tool] bash: ls" in digest.getvalue()


def test_consume_event_turn_timing(tmp_path, monkeypatch):
    """ターン時間 / 生成 (思考) 時間 / TTFT の積算を fake clock で検証。"""
    import io
    mgr = _manager(tmp_path, _FakeManager(0))
    run = {"id": "cb_t", "progress": {"turns": 0, "tool_calls": 0, "tool_errors": 0,
                                      "tokens_in": 0, "tokens_out": 0, "text": "",
                                      "last_tool": "", "gen_avg": None}}
    stats = {"turns": 0, "tool_calls": 0, "tool_errors": 0, "tokens_in": 0, "tokens_out": 0,
             "turn_secs_sum": 0.0, "turn_n": 0, "gen_secs_sum": 0.0, "gen_n": 0,
             "ttft_sum": 0.0, "ttft_n": 0}
    clock = [100.0]
    monkeypatch.setattr(coding_mod.time, "monotonic", lambda: clock[0])
    digest = io.StringIO()
    mgr._consume_event(run, "backend", {"type": "turn_start"}, digest, stats)
    clock[0] = 100.5
    mgr._consume_event(run, "backend", {"type": "message_start",
                                        "message": {"role": "assistant"}}, digest, stats)
    clock[0] = 101.0
    mgr._consume_event(run, "backend", {"type": "message_update",
                                        "usage": {"input": 10, "output": 1}}, digest, stats)
    clock[0] = 103.0
    mgr._consume_event(run, "backend", {"type": "message_end", "message": {
        "role": "assistant", "usage": {"input": 100, "output": 20},
        "content": [{"type": "text", "text": "ok"}]}}, digest, stats)
    clock[0] = 104.0
    mgr._consume_event(run, "backend", {"type": "turn_end"}, digest, stats)
    assert stats["turn_n"] == 1 and abs(stats["turn_secs_sum"] - 4.0) < 1e-9
    assert stats["gen_n"] == 1 and abs(stats["gen_secs_sum"] - 2.5) < 1e-9
    assert stats["ttft_n"] == 1 and abs(stats["ttft_sum"] - 0.5) < 1e-9
    assert run["progress"]["gen_avg"] == 2.5
    # user メッセージの message_start は生成時間に含めない
    mgr._consume_event(run, "backend", {"type": "message_start",
                                        "message": {"role": "user"}}, digest, stats)
    assert "_msg_t0" not in stats


# ------------------------------------------------------------------ 5. ファイル参照


def test_safe_join_traversal(tmp_path):
    base = tmp_path / "ws"
    base.mkdir()
    (base / "a.txt").write_text("x")
    assert coding_mod.safe_join(base, "a.txt") == base / "a.txt"
    assert coding_mod.safe_join(base, "") == base
    for bad in ("../secret", "a/../../secret", "a/../../../etc/passwd"):
        with pytest.raises(CodingError):
            coding_mod.safe_join(base, bad)
    # 先頭スラッシュは URL 仕様と同じくワークスペース直下扱い (外へは出ない)
    assert coding_mod.safe_join(base, "/a.txt") == base / "a.txt"


def test_file_tree_skips_junk(tmp_path):
    ws = tmp_path / "ws"
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "index.js").write_text("x")
    (ws / "backend").mkdir()
    (ws / "backend" / "server.py").write_text("x")
    tree = coding_mod.file_tree(ws)
    assert [f["path"] for f in tree] == ["backend/server.py"]
    scan = coding_mod.scan_workspace(ws)
    assert scan["count"] == 1


# ------------------------------------------------------------------ 6. プレビュー


def test_preview_detect_and_app_lifecycle(tmp_path, server):
    _, port = server
    mgr = _manager(tmp_path, _FakeManager(port), _write_fake_pi(tmp_path))
    run = _run_to_finish(mgr, _params(mgr))
    rid = run["id"]
    det = mgr.detect_preview(mgr.run_dir(rid) / "workspace")
    assert det == {"mode": "app", "entry": "backend/server.py"}

    async def go() -> dict:
        info = await mgr.preview_start(rid)
        assert info["mode"] == "app" and info["port"]
        # 生成アプリが実際に応答する (PORT 環境変数で渡したポート)
        def _probe() -> dict:
            with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/api/health",
                                        timeout=5) as r:
                return json.loads(r.read())
        assert (await asyncio.to_thread(_probe))["ok"] is True
        assert mgr.preview_info(rid)["port"] == info["port"]
        await mgr.preview_stop(rid)
        assert mgr.preview_info(rid) is None
        assert mgr.get(rid)["preview"] is None
        return info
    asyncio.run(go())


def test_preview_static_fallback(tmp_path, server):
    _, port = server
    mgr = _manager(tmp_path, _FakeManager(port), _write_fake_pi(tmp_path))
    run = _run_to_finish(mgr, _params(mgr))
    rid = run["id"]
    ws = mgr.run_dir(rid) / "workspace"
    (ws / "backend" / "server.py").unlink()      # バックエンド無し → 静的配信
    det = mgr.detect_preview(ws)
    assert det["mode"] == "static"

    async def go() -> None:
        info = await mgr.preview_start(rid)
        assert info["mode"] == "static"
        await mgr.preview_stop(rid)
    asyncio.run(go())


def test_preview_nothing_to_show(tmp_path):
    mgr = _manager(tmp_path, _FakeManager(0))
    rid = "cb_none"
    (mgr.run_dir(rid) / "workspace").mkdir(parents=True)
    mgr.store.mutate(lambda lst: lst.append(
        {"id": rid, "status": "completed", "created_at": 0, "params": {}}))
    with pytest.raises(CodingError, match="プレビューできる実装が見つかりません"):
        asyncio.run(mgr.preview_start(rid))


# ------------------------------------------------------------------ 7. 履歴 / 回復


def test_delete_removes_files_and_clear_keeps_active(tmp_path):
    mgr = _manager(tmp_path, _FakeManager(0))
    for i, st in enumerate(("completed", "failed", "running")):
        rid = f"cb_{i}"
        (mgr.run_dir(rid) / "workspace").mkdir(parents=True)
        mgr.store.mutate(lambda lst, rid=rid, st=st, i=i: lst.append(
            {"id": rid, "status": st, "created_at": i, "params": {}}))
    mgr.active_id = "cb_2"
    assert mgr.clear_all() == 2
    assert not mgr.run_dir("cb_0").exists()
    assert mgr.run_dir("cb_2").exists()
    mgr.active_id = None
    # 実行中 (running) のうちは削除拒否 → 完了にしてから削除
    with pytest.raises(CodingError):
        mgr.delete("cb_2")
    mgr.store.mutate(lambda lst: [r.update({"status": "completed"})
                                  for r in lst if r["id"] == "cb_2"])
    mgr.delete("cb_2")
    assert not mgr.run_dir("cb_2").exists()
    assert mgr.get("cb_2") is None
    # 削除済みは _save で復活しない
    mgr._save({"id": "cb_2", "status": "completed"}, force=True)
    assert mgr.get("cb_2") is None


def test_on_startup_marks_lost_and_clears_preview(tmp_path):
    mgr = _manager(tmp_path, _FakeManager(0))
    mgr.store.mutate(lambda lst: lst.extend([
        {"id": "cb_r", "status": "running", "created_at": 0, "params": {},
         "preview": {"mode": "app", "port": 1234}},
        {"id": "cb_c", "status": "completed", "created_at": 1, "params": {}},
    ]))
    lost = mgr.on_startup()
    assert lost == ["cb_r"]
    assert mgr.get("cb_r")["status"] == "lost"
    assert mgr.get("cb_r")["preview"] is None
    assert mgr.get("cb_c")["status"] == "completed"


# ------------------------------------------------------------------ 8. API


def test_thinking_capability_detection():
    """thinking はモデルで設定できるものだけ: parser 設定 / モデル名で判定。"""
    cap = coding_mod.thinking_capability(["--reasoning-parser", "qwen3"], "")
    assert cap["capable"] and cap["qwen"] and cap["levels"] == ["off", "on"]
    assert "reasoning-parser" in cap["source"]
    cap = coding_mod.thinking_capability([], "Qwen3-30B-A3B-Instruct")
    assert cap["capable"] and cap["qwen"]
    cap = coding_mod.thinking_capability([], "Inkling-2-Mini-UD-Q8_K_XL.gguf")
    assert cap["capable"] and not cap["qwen"] and cap["levels"] == ["off", "on"]
    cap = coding_mod.thinking_capability(["--reasoning", "on"], "some-model")
    assert cap["capable"]
    cap = coding_mod.thinking_capability([], "Llama-3.1-8B-Instruct")
    assert not cap["capable"] and cap["levels"] == ["off"]
    assert coding_mod.argv_flag(["--reasoning-parser=glm45"], "--reasoning-parser") == "glm45"


def test_model_ids_and_pi_home_qwen_wiring(tmp_path, server):
    _, port = server
    ids = coding_mod.model_ids(f"http://127.0.0.1:{port}")
    assert ids == ["test-model"]
    assert coding_mod.model_ids("http://127.0.0.1:1") == []   # 接続失敗は []
    mgr = _manager(tmp_path, _FakeManager(port))
    rd = mgr.run_dir("cb_q")
    rd.mkdir(parents=True)
    mgr._prepare_pi_home(rd, f"http://127.0.0.1:{port}", "Qwen3-30B-A3B", "",
                         {"thinking": "off"})
    entry = json.loads((rd / "pi-home" / "models.json").read_text())["providers"]["launcher"]["models"][0]
    # Qwen3 系は off でも enable_thinking=false を送れる配線 (reasoning=True + format)
    assert entry["reasoning"] is True
    assert entry["compat"]["thinkingFormat"] == "qwen-chat-template"
    assert entry["thinkingLevelMap"]["medium"] is None
    mgr._prepare_pi_home(rd, f"http://127.0.0.1:{port}", "Llama-3.1-8B", "",
                         {"thinking": "off"})
    entry = json.loads((rd / "pi-home" / "models.json").read_text())["providers"]["launcher"]["models"][0]
    assert entry["reasoning"] is False and "compat" not in entry


def test_thinking_capability_uses_server_effort_vocabulary():
    """サーバーが effort 語彙を公開すればファミリーを問わずその段階を出す。
    語彙はサーバーがチェックポイントのテンプレートを probe した結果: Inkling-2 は
    "none" 含む、Qwen3.8-Flash-Next は "none" 無しの xhigh/medium/low
    (off は enable_thinking トグル)。語彙の無い Qwen3 は従来の off/on。"""
    eff = ["max", "xhigh", "high", "medium", "low", "minimal", "none"]
    cap = coding_mod.thinking_capability([], "Inkling-2-Mini", eff, "high")
    assert cap["capable"] and not cap["qwen"]
    assert cap["levels"] == ["off", "max", "xhigh", "high", "medium", "low", "minimal"]
    assert cap["default"] == "high"
    cap = coding_mod.thinking_capability(["--reasoning-parser", "glm"], "some-model",
                                         ["xhigh", "medium", "low"], "xhigh")
    assert cap["capable"] and cap["levels"] == ["off", "xhigh", "medium", "low"]
    cap = coding_mod.thinking_capability([], "RadixArk--Qwen3.8-Flash-Next-NVFP4",
                                         ["xhigh", "medium", "low"], "xhigh")
    assert cap["capable"] and cap["qwen"]
    assert cap["levels"] == ["off", "xhigh", "medium", "low"]
    assert cap["default"] == "xhigh"
    cap = coding_mod.thinking_capability([], "Qwen3-30B")
    assert cap["qwen"] and cap["levels"] == ["off", "on"]


def test_model_efforts_reads_v1_models(effort_server):
    _, port = effort_server
    eff, default = coding_mod.model_efforts(f"http://127.0.0.1:{port}")
    assert "none" in eff and default == "high"
    assert coding_mod.model_efforts("http://127.0.0.1:1") == ([], None)


def test_pi_home_effort_wiring(tmp_path, effort_server):
    """effort 配線: off でも reasoning=True にして pi が reasoning_effort="none" を
    送れるようにする (Inkling-2 のテンプレート既定 0.9 を確実に切る)。"""
    _, port = effort_server
    eff, _ = coding_mod.model_efforts(f"http://127.0.0.1:{port}")
    mgr = _manager(tmp_path, _FakeManager(port))
    rd = mgr.run_dir("cb_e")
    rd.mkdir(parents=True)
    mgr._prepare_pi_home(rd, f"http://127.0.0.1:{port}", "Inkling-2-Mini", "",
                         {"thinking": "off"}, eff)
    entry = json.loads((rd / "pi-home" / "models.json").read_text())["providers"]["launcher"]["models"][0]
    assert entry["reasoning"] is True
    assert entry["compat"]["supportsReasoningEffort"] is True
    # pi が developer ロールを送らないようにする (Inkling テンプレートは system のみ)
    assert entry["compat"]["supportsDeveloperRole"] is False
    assert entry["thinkingLevelMap"]["off"] == "none"
    assert entry["thinkingLevelMap"]["xhigh"] == "xhigh"
    assert entry["thinkingLevelMap"]["max"] == "max"
    # 思考 OFF は 8192、思考 ON は thinking が出力枠を食うため 32768 に広げる
    # (8192 固定だと thinking=max が length 打ち切りでツールを呼べず終了する)。
    assert entry["maxTokens"] == 8192
    mgr._prepare_pi_home(rd, f"http://127.0.0.1:{port}", "Inkling-2-Mini", "",
                         {"thinking": "max"}, eff)
    entry = json.loads((rd / "pi-home" / "models.json").read_text())["providers"]["launcher"]["models"][0]
    assert entry["maxTokens"] == 32768


def test_pi_home_qwen_effort_wiring(tmp_path, server):
    """Qwen3.8-Flash-Next: enable_thinking トグルと effort 段階の共存配線。
    qwen-chat-template は enable_thinking しか送れないため chat-template で
    chat_template_kwargs に両方を送る。off は reasoning_effort を omit
    (テンプレート既定の xhigh が漏れないように)。"""
    _, port = server
    eff = ["xhigh", "medium", "low"]
    mgr = _manager(tmp_path, _FakeManager(port))
    rd = mgr.run_dir("cb_qe")
    rd.mkdir(parents=True)
    mgr._prepare_pi_home(rd, f"http://127.0.0.1:{port}", "Qwen3.8-Flash-Next-NVFP4", "",
                         {"thinking": "xhigh"}, eff)
    entry = json.loads((rd / "pi-home" / "models.json").read_text())["providers"]["launcher"]["models"][0]
    assert entry["reasoning"] is True
    assert entry["compat"]["thinkingFormat"] == "chat-template"
    kw = entry["compat"]["chatTemplateKwargs"]
    assert kw["enable_thinking"] == {"$var": "thinking.enabled"}
    assert kw["reasoning_effort"] == {"$var": "thinking.effort", "omitWhenOff": True}
    lm = entry["thinkingLevelMap"]
    assert lm["xhigh"] == "xhigh" and lm["medium"] == "medium" and lm["low"] == "low"
    assert lm["minimal"] is None and lm["max"] is None
    assert lm["high"] == "xhigh"      # 旧 "on" の読み替え先 (サーバーの量子化と同じ)
    assert "off" not in lm            # off は enable_thinking=false で送る (unsupported ではない)
    assert entry["maxTokens"] == 32768
    mgr._prepare_pi_home(rd, f"http://127.0.0.1:{port}", "Qwen3.8-Flash-Next-NVFP4", "",
                         {"thinking": "off"}, eff)
    entry = json.loads((rd / "pi-home" / "models.json").read_text())["providers"]["launcher"]["models"][0]
    assert entry["maxTokens"] == 8192


def test_api_model_options(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    JsonStore(tmp_path / "data" / "profiles.json", []).mutate(lambda lst: lst.append(PROFILE))
    client = Client(_app(tmp_path))

    async def go() -> None:
        st, r = await client.get("/api/coding/model-options?profile_id=p1")
        assert st == 200
        assert r["live"] is False and r["models"] == []
        assert r["thinking"]["levels"] == ["off"]      # 非思考モデルは off のみ
        st, r = await client.get("/api/coding/model-options?profile_id=nope")
        assert st == 404
    asyncio.run(go())


def test_api_model_options_live_server(tmp_path, server, monkeypatch):
    """稼働中セッションがいれば /v1/models からモデル名を自動取得する。"""
    from llm_launcher import app as app_mod
    _, port = server
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    JsonStore(tmp_path / "data" / "profiles.json", []).mutate(lambda lst: lst.append(PROFILE))

    class _Mgr(app_mod.ProcessManager):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.sessions["s1"] = {"id": "s1", "profile_id": "p1", "status": "running",
                                   "port": port, "health_path": "/health", "argv": []}
    monkeypatch.setattr(app_mod, "ProcessManager", _Mgr)
    client = Client(app_mod.create_app(tmp_path))

    async def go() -> None:
        st, r = await client.get("/api/coding/model-options?profile_id=p1")
        assert st == 200
        assert r["live"] is True
        assert r["models"] == ["test-model"] and r["model"] == "test-model"
    asyncio.run(go())


def _app(tmp_path):
    from llm_launcher import app as app_mod
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return app_mod.create_app(tmp_path)


def test_api_assets_and_empty_runs(tmp_path):
    client = Client(_app(tmp_path))

    async def go() -> None:
        st, a = await client.get("/api/coding/assets")
        assert st == 200
        assert Path(a["agents_path"]).is_file()
        assert len(a["skills"]) == 3
        st, r = await client.get("/api/coding/runs")
        assert st == 200 and r["runs"] == [] and r["active"] is None
        st, s = await client.get("/api/coding/status")
        assert st == 200 and "pi" in s and "sandbox" in s
    asyncio.run(go())


def test_api_start_validation(tmp_path, monkeypatch):
    # プロファイルは create_app より前に置く (アプリ側ストアのキャッシュ対策)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    JsonStore(tmp_path / "data" / "profiles.json", []).mutate(lambda lst: lst.append(PROFILE))
    client = Client(_app(tmp_path))

    async def go() -> None:
        st, r = await client.post("/api/coding/runs", {"profile_id": "nope"})
        assert st == 404
        st, r = await client.post("/api/coding/runs",
                                  {"profile_id": "p1", "skills_dir": "/no/such/dir"})
        assert st == 422 and "スキルフォルダ" in r["detail"]
        st, r = await client.post("/api/coding/runs",
                                  {"profile_id": "p1", "agents_path": "/no/such/file.md"})
        assert st == 422 and "AGENTS.md" in r["detail"]
        st, r = await client.post("/api/coding/runs",
                                  {"profile_id": "p1", "thinking": "ultra"})
        assert st == 422
        # pi 未導入 → 409 (ユーザーに導入を促す)
        monkeypatch.setattr(PiTool, "command", lambda self: "")
        st, r = await client.post("/api/coding/runs", {"profile_id": "p1"})
        assert st == 409 and "pi" in r["detail"]
    asyncio.run(go())


def _seed_completed_run(tmp_path, rid="cb_api", with_backend=True):
    """完了済み run + workspace を直接用意 (API 参照系のテスト用)。"""
    assets = CodingAssets(tmp_path)
    assets.ensure_defaults()
    ws = assets.runs_dir / rid / "workspace"
    (ws / "frontend").mkdir(parents=True)
    (ws / "frontend" / "index.html").write_text("<h1>hello</h1>", encoding="utf-8")
    if with_backend:
        (ws / "backend").mkdir()
        (ws / "backend" / "server.py").write_text("# entry", encoding="utf-8")
    (ws / "AGENTS.md").write_text("rules", encoding="utf-8")
    store = JsonStore(tmp_path / "data" / "coding.json", [])
    store.mutate(lambda lst: lst.append({
        "id": rid, "name": "api run", "status": "completed", "created_at": 1,
        "profile_id": "p1", "profile_name": "p", "backend": "vllm", "backend_name": "vLLM",
        "model": "m", "params": {"sandbox": False}, "phases": {},
        "files": {"count": 3, "bytes": 30}, "preview": None,
    }))
    return ws


def test_api_files_file_log_and_traversal(tmp_path):
    ws = _seed_completed_run(tmp_path)
    (ws / "bin.dat").write_bytes(b"\xff\xfe\x00")   # 不正 UTF-8 = バイナリ扱い
    client = Client(_app(tmp_path))

    async def go() -> None:
        st, r = await client.get("/api/coding/runs/cb_api/files")
        assert st == 200
        paths = {f["path"] for f in r["files"]}
        assert {"frontend/index.html", "backend/server.py"} <= paths
        st, r = await client.get("/api/coding/runs/cb_api/file?path=frontend/index.html")
        assert st == 200 and "<h1>hello</h1>" in r["text"]
        st, r = await client.get("/api/coding/runs/cb_api/file?path=../../etc/passwd")
        assert st in (403, 404)
        st, r = await client.get("/api/coding/runs/cb_api/file?path=bin.dat")
        assert st == 404 and "バイナリ" in r["detail"]
        st, r = await client.get("/api/coding/runs/cb_api/log?phase=backend")
        assert st == 200 and r["phase"] == "backend"
        st, r = await client.get("/api/coding/runs/cb_api/log?phase=bogus")
        assert st == 422
        st, r = await client.get("/api/coding/runs/missing/files")
        assert st == 404
    asyncio.run(go())


def test_api_static_preview_and_proxy_guard(tmp_path):
    _seed_completed_run(tmp_path, with_backend=False)
    client = Client(_app(tmp_path))

    async def go() -> None:
        # バックエンド無し → 静的配信プレビュー
        st, r = await client.post("/api/coding/runs/cb_api/preview")
        assert st == 200 and r["mode"] == "static"
        st, raw = await client.get("/api/coding/runs/cb_api/static/")
        assert st == 200 and b"hello" in raw
        st, raw = await client.get("/api/coding/runs/cb_api/static/index.html")
        assert st == 200
        st, r = await client.get("/api/coding/runs/cb_api/static/nope.js")
        assert st == 404
        # プロキシはプレビュー起動が必要 (static モードは port 無し)
        st, r = await client.get("/api/coding/runs/cb_api/proxy/api/health")
        assert st == 409
        st, r = await client.delete("/api/coding/runs/cb_api/preview")
        assert st == 200
    asyncio.run(go())


def test_api_proxy_forwards_to_preview_port(tmp_path, server, monkeypatch):
    _, port = server
    _seed_completed_run(tmp_path)
    monkeypatch.setattr(CodingManager, "preview_info",
                        lambda self, rid: {"port": port, "mode": "app",
                                           "command": "python3 backend/server.py",
                                           "started_at": 0, "pid": 1})
    client = Client(_app(tmp_path))

    async def go() -> None:
        st, raw = await client.get("/api/coding/runs/cb_api/proxy/health")
        assert st == 200 and raw["ok"] is True
        st, raw = await client.get("/api/coding/runs/cb_api/proxy/v1/models")
        assert st == 200 and raw["data"][0]["id"] == "test-model"
        st, raw = await client.get("/api/coding/runs/cb_api/proxy/missing")
        assert st == 404
    asyncio.run(go())


def test_api_delete_and_clear(tmp_path):
    _seed_completed_run(tmp_path, rid="cb_d1")
    _seed_completed_run(tmp_path, rid="cb_d2")
    client = Client(_app(tmp_path))

    async def go() -> None:
        st, r = await client.delete("/api/coding/runs/cb_d1")
        assert st == 200
        assert not (CodingAssets(tmp_path).runs_dir / "cb_d1").exists()
        st, r = await client.delete("/api/coding/runs")
        assert st == 200 and r["removed"] == 1
        st, r = await client.get("/api/coding/runs")
        assert r["runs"] == []
        st, r = await client.delete("/api/coding/runs/cb_d1")
        assert st == 404
    asyncio.run(go())


def test_rbac_coding_uses_bench_cap():
    for path in ("/api/coding/runs", "/api/coding/assets", "/api/coding/status",
                 "/api/coding/runs/x/preview", "/api/coding/runs/x/proxy/",
                 "/api/coding/runs/x/static/index.html", "/api/coding/pi/install"):
        assert rbac.required_cap("GET", path) == "bench"
    assert rbac.required_cap("POST", "/api/coding/runs") == "bench"


# ------------------------------------------------------------------ 9. 実 pi + bwrap E2E
# pi と bubblewrap が両方ある環境でのみ走る統合テスト。偽 OpenAI 互換サーバーが
# SSE で「完了」テキストを返し、実 pi がサンドボックス内で起動 → models.json 認証 →
# JSONL 出力 → セッション保存まで通ることを確認する。


class _SseHandler(BaseHTTPRequestHandler):
    """pi (openai-completions) 向けの最小 SSE 応答サーバー。"""

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            body = b'{"ok":true}'
        elif self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "test-model"}]}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk1 = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
                  "model": "test-model",
                  "choices": [{"index": 0, "delta": {"role": "assistant",
                                                     "content": "完了しました"},
                               "finish_reason": None}]}
        chunk2 = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
                  "model": "test-model",
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                            "total_tokens": 15}}
        for c in (chunk1, chunk2):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


def test_real_pi_in_sandbox_e2e(tmp_path):
    import shutil
    import tempfile
    settings = JsonStore(tmp_path / "data" / "settings.json", {})
    pi = PiTool(settings, tmp_path / "pi-install.log")
    if not pi.available() or not coding_mod.sandbox_backend():
        pytest.skip("pi またはサンドボックス (bwrap / docker) が使えない環境ではスキップ")
    # docker daemon は /tmp を共有しないことがある (systemd PrivateTmp)。
    # run ディレクトリは daemon から見えるリポジトリ配下 (.scratch/) に置く。
    scratch = Path(__file__).resolve().parents[1] / ".scratch"
    scratch.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=scratch, prefix="llmcb-e2e-"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SseHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        mgr = _manager(base, _FakeManager(port))
        run = _run_to_finish(mgr, _params(mgr, sandbox=True, phase_timeout=120))
        assert run["status"] == "completed", run["error"]
        rd = mgr.run_dir(run["id"])
        assert (rd / "sessions").is_dir()
        assert any((rd / "sessions").rglob("*.jsonl"))     # pi セッションが保存された
        log = (rd / "logs" / "backend.log").read_text(encoding="utf-8")
        assert "完了しました" in log
        assert run["phases"]["backend"]["exit_code"] == 0
        assert run["phases"]["frontend"]["exit_code"] == 0
        # プレビューもサンドボックス内で起動できる (生成アプリのふりをする server.py)
        ws = rd / "workspace"
        (ws / "backend").mkdir(exist_ok=True)
        (ws / "backend" / "server.py").write_text(
            "import json, os\n"
            "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({'ok': True}).encode()\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type', 'application/json')\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "    def log_message(self, *a):\n"
            "        pass\n"
            "ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PORT', '8000'))), H)"
            ".serve_forever()\n", encoding="utf-8")

        async def preview() -> None:
            info = await mgr.preview_start(run["id"])
            assert info["mode"] == "app" and info["port"]

            def _probe() -> dict:
                with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/",
                                            timeout=5) as r:
                    return json.loads(r.read())
            try:
                assert (await asyncio.to_thread(_probe))["ok"] is True
            finally:
                await mgr.preview_stop(run["id"])
            assert mgr.preview_info(run["id"]) is None
        asyncio.run(preview())
    finally:
        srv.shutdown()
        shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------------------------ 10. フロントエンド契約


def _frontend_src() -> str:
    base = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static"
    parts = [(base / "index.html").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted((base / "css").glob("*.css"))]
    parts += [p.read_text(encoding="utf-8") for p in sorted((base / "js").rglob("*.js"))]
    return "\n".join(parts)


def test_frontend_contract():
    src = _frontend_src()
    assert "VIEWS.coding" in src
    # タブ統一: コーディングベンチは独立メニューを持たず、ベンチマーク内のモード
    assert 'data-tab="coding"' not in src                  # 旧メニュー項目は廃止
    assert 'id="mi-coding"' not in src
    assert 'id="nb-coding"' not in src
    assert "bm-mode-coding" in src                         # ベンチマーク内の受け皿
    assert "bmSetMode" in src                              # 速度 / コーディング切替
    assert "codingVisible" in src
    assert "views/coding.js" in src                        # 読み込み
    assert '"coding"' in src.split("ROUTE_TABS")[1][:200]  # 旧 URL 互換エイリアス
    # 実行フォーム: プロファイル / スキル / AGENTS.md / 2 プロンプト
    for el in ("cb-profile", "cb-skills", "cb-agents", "cb-backend", "cb-frontend",
               "cb-sandbox", "cb-pi-install"):
        assert el in src, el
    # ビューアー: 2 実装並列 + iframe + 履歴から選択して起動
    for el in ("cb-grid", "cb-frame", "cb-selA", "cb-selB", "cbViewRun",
               "cbPreviewStart", "cb-pane", "cb-warn", "cbMetricsTable",
               "cbCmpToggle", "cb-table", "cb-best", "cbLoadModelOptions",
               "cb-think-note"):
        assert el in src, el
    assert ".cb-grid" in src                               # CSS 2 カラム
    # 出力履歴 (モデルが出力したトークン内容) を履歴から閲覧
    for el in ("cbToggleTranscript", "cbTranscriptPanel", "cbTxPhase", "cb-tx",
               "/transcript?phase="):
        assert el in src, el
    # チェック即時反映: フォーカス中でも強制描画 + in-place 同期
    assert "cbCmpSync" in src
    assert "renderCodingHistory(true)" in src
    # 完了カードの消し残し対策: live カードはキャッシュなしで直接クリア
    assert 'el.innerHTML = ""' in src

# ------------------------------------------------------------------ 11. DSH 風ツール拡張
# pi に持たせるファイル編集 / 検索 / 閲覧ツール (read / edit / write / grep / glob)。
# 既定資材として生成され、-e で明示ロードされる (--no-extensions は自動発見だけ止める)。


def test_extension_asset_created_and_idempotent(tmp_path):
    assets = CodingAssets(tmp_path)
    assets.ensure_defaults()
    ext = assets.extension_path
    assert ext.is_file()
    text = ext.read_text(encoding="utf-8")
    for name in ("read", "edit", "write", "grep", "glob"):
        assert f'name: "{name}"' in text, name
    assert "withFileMutationQueue" in text
    assert "replace_all" in text and "old_string" in text
    # ユーザー編集は上書きしない
    ext.write_text("// custom", encoding="utf-8")
    assets.ensure_defaults()
    assert ext.read_text(encoding="utf-8") == "// custom"
    assert str(ext) in assets.defaults_info()["extension_path"]


def _argv_for(mgr, tmp_path, sandbox=False, monkeypatch=None):
    rd = mgr.run_dir("cb_argv")
    ws = rd / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    run = {"id": "cb_argv", "name": "n", "model": "test-model"}
    params = _params(mgr, sandbox=sandbox)
    skill_dirs = mgr._skill_dirs(Path(params["skills_dir"]))
    return mgr._pi_argv(run, "backend", "p", rd, ws, skill_dirs, params)


def test_pi_argv_wires_extension(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, _FakeManager(0))
    argv, env, container = _argv_for(mgr, tmp_path)
    assert "--extension" in argv
    ext = str(mgr.assets.extension_path)
    assert argv[argv.index("--extension") + 1] == ext
    assert argv[argv.index("--tools") + 1] == "read,bash,edit,write,grep,glob"
    assert "--no-extensions" in argv          # 自動発見は止めたまま明示 -e は有効
    assert container == ""
    # サンドボックス (bwrap / docker) には拡張ファイルが ro で渡る
    monkeypatch.setattr(coding_mod, "sandbox_backend", lambda settings=None: "bwrap")
    argv, _, _ = _argv_for(mgr, tmp_path, sandbox=True)
    i = argv.index(ext)
    assert argv[i - 1] == "--ro-bind"
    monkeypatch.setattr(coding_mod, "sandbox_backend", lambda settings=None: "docker")
    argv, _, container = _argv_for(mgr, tmp_path, sandbox=True)
    assert f"{ext}:{ext}:ro" in argv
    assert container.startswith(coding_mod.CONTAINER_PREFIX)


def test_pi_argv_fallback_without_extension(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, _FakeManager(0))
    # _params → defaults_info が資材を再生成してしまうので ensure_defaults を止めてから消す
    monkeypatch.setattr(CodingAssets, "ensure_defaults", lambda self: None)
    mgr.assets.extension_path.unlink()
    argv, _, _ = _argv_for(mgr, tmp_path)
    assert "--extension" not in argv
    assert argv[argv.index("--tools") + 1] == "read,bash,edit,write,grep,find,ls"


# ------------------------------------------------------------------ 12. 出力履歴 (transcript)


def _seed_transcript_log(tmp_path, rid="cb_tx"):
    logs = CodingAssets(tmp_path).runs_dir / rid / "logs"
    logs.mkdir(parents=True)
    events = [
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "user", "content": "依頼"}},
        {"type": "message_end", "message": {
            "role": "assistant", "timestamp": 1795507167913,
            "stopReason": "toolUse", "usage": {"input": 100, "output": 40},
            "content": [
                {"type": "thinking", "thinking": "まずファイルを作る"},
                {"type": "text", "text": "作成します"},
                {"type": "toolCall", "id": "c1", "name": "write",
                 "arguments": {"file_path": "a.txt", "content": "hello"}},
            ]}},
        {"type": "message_update", "usage": {"input": 1, "output": 1}},
        {"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop",
            "usage": {"input": 200, "output": 10},
            "content": [{"type": "text", "text": "完了しました"}]}},
        {"type": "agent_end"},
    ]
    with open(logs / "backend.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        f.write("{broken json\n")          # 壊れた行は読み飛ばす
    return logs


def test_transcript_parses_model_output(tmp_path):
    mgr = _manager(tmp_path, _FakeManager(0))
    _seed_transcript_log(tmp_path)
    r = mgr.transcript("cb_tx", "backend")
    assert r["missing"] is False and r["total"] == 2 and not r["truncated"]
    m1, m2 = r["messages"]
    assert m1["thinking"] == "まずファイルを作る"
    assert m1["text"] == "作成します"
    assert m1["tools"] == [{"name": "write", "args": {"file_path": "a.txt", "content": "hello"}}]
    assert m1["tokens_out"] == 40 and m1["stop_reason"] == "toolUse"
    assert m1["ts"] == 1795507167913
    assert m2["text"] == "完了しました" and m2["tools"] == []
    # 記録なし / 不正 phase
    assert mgr.transcript("cb_tx", "frontend")["missing"] is True
    with pytest.raises(CodingError):
        mgr.transcript("cb_tx", "preview")


def test_transcript_budget_truncation(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, _FakeManager(0))
    _seed_transcript_log(tmp_path)
    monkeypatch.setattr(coding_mod, "TRANSCRIPT_MAX_BYTES", 10)
    r = mgr.transcript("cb_tx", "backend")
    assert r["truncated"] is True
    assert r["total"] == 2                    # 件数は最後まで数える
    assert len(r["messages"]) == 1            # 先頭 1 件は冒頭だけ見せる
    assert r["messages"][0]["clipped"] is True


def test_api_transcript(tmp_path):
    _seed_completed_run(tmp_path, rid="cb_txapi")
    _seed_transcript_log(tmp_path, rid="cb_txapi")
    client = Client(_app(tmp_path))

    async def go() -> None:
        st, r = await client.get("/api/coding/runs/cb_txapi/transcript?phase=backend")
        assert st == 200
        assert r["total"] == 2 and r["messages"][1]["text"] == "完了しました"
        st, r = await client.get("/api/coding/runs/cb_txapi/transcript?phase=bogus")
        assert st == 422
        st, r = await client.get("/api/coding/runs/missing/transcript")
        assert st == 404
    asyncio.run(go())


# ------------------------------------------------------------------ 13. 実 pi + DSH ツール E2E
# 偽 OpenAI 互換サーバーが write → read → edit → grep → glob → 完了テキスト の順に
# ツール呼び出しを指示し、実 pi がサンドボックス内で DSH 風ツールを実行できることを
# 確認する (拡張のロード / typebox alias / ファイルミューテーションまで通る)。


class _ToolScriptHandler(BaseHTTPRequestHandler):
    """会話中の tool 結果数で次の一手を決めるスクリプト型 SSE サーバー。"""

    STEPS = [
        ("write", {"file_path": "hello.txt", "content": "alpha\nbeta\n"}),
        ("read", {"file_path": "hello.txt"}),
        ("edit", {"file_path": "hello.txt", "old_string": "beta", "new_string": "gamma"}),
        ("grep", {"pattern": "gamma", "include": "*.txt"}),
        ("glob", {"pattern": "**/*.txt"}),
    ]

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            body = b'{"ok":true}'
        elif self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "test-model"}]}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        step = sum(1 for m in req.get("messages", []) if m.get("role") == "tool")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if step < len(self.STEPS):
            name, args = self.STEPS[step]
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"call_{step}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
            fin = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "ALL_DONE"}
            fin = "stop"
        c1 = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
              "model": "test-model",
              "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        c2 = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
              "model": "test-model",
              "choices": [{"index": 0, "delta": {}, "finish_reason": fin}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        for c in (c1, c2):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


def test_real_pi_dsh_tools_e2e(tmp_path):
    import shutil
    import tempfile
    settings = JsonStore(tmp_path / "data" / "settings.json", {})
    pi = PiTool(settings, tmp_path / "pi-install.log")
    if not pi.available() or not coding_mod.sandbox_backend():
        pytest.skip("pi またはサンドボックス (bwrap / docker) が使えない環境ではスキップ")
    scratch = Path(__file__).resolve().parents[1] / ".scratch"
    scratch.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=scratch, prefix="llmcb-tools-"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ToolScriptHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        mgr = _manager(base, _FakeManager(port))
        run = _run_to_finish(mgr, _params(mgr, sandbox=True, phase_timeout=180))
        assert run["status"] == "completed", run["error"]
        ws = mgr.run_dir(run["id"]) / "workspace"
        # write → edit が実際にファイルへ反映 (DSH 風リテラル置換)
        assert (ws / "hello.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"
        st = run["phases"]["backend"]
        assert st["tool_errors"] == 0, st
        assert st["tool_calls"] >= 5
        # --session-id 廃止: pi の "No project session found with id 'coding'" 警告が
        # backend / frontend どちらの stderr にも出ない (backend は id 指定なしで新規作成、
        # frontend は --continue で継続)。セッションファイルも 1 つだけ (継続の証拠)。
        logs = mgr.run_dir(run["id"]) / "logs"
        for ph in ("backend", "frontend"):
            err = (logs / f"{ph}.stderr").read_text(encoding="utf-8")
            assert "No project session found" not in err, err
        assert len(list((mgr.run_dir(run["id"]) / "sessions").glob("*.jsonl"))) == 1
        # 出力履歴にモデルのツール引数と最終テキストが残る
        tx = mgr.transcript(run["id"], "backend")
        names = [t["name"] for m in tx["messages"] for t in m["tools"]]
        assert names[:5] == ["write", "read", "edit", "grep", "glob"]
        assert any(m["text"] == "ALL_DONE" for m in tx["messages"])
        write_args = next(t["args"] for m in tx["messages"] for t in m["tools"]
                          if t["name"] == "write")
        assert write_args["content"] == "alpha\nbeta\n"
    finally:
        srv.shutdown()
        shutil.rmtree(base, ignore_errors=True)
