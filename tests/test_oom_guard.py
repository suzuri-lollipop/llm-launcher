"""OOM 対策モジュール (llm_launcher.oom) と起動前メモリガードのテスト。

/proc・/sys/fs/cgroup は tmp に作った偽ディレクトリに差し替えて読むだけ読み、
書き込み系 (oom_score_adj) は特権が必要なので関数ごとモックして検証する。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_launcher import oom, process as process_mod
from llm_launcher.oom import MemoryGuardError
from llm_launcher.process import ProcessManager
from llm_launcher.store import JsonStore

GB = 1 << 30
MEMINFO = ("MemTotal:       32000000 kB\nMemFree:   4000000 kB\n"
           "MemAvailable: {avail} kB\nSwapTotal:  8000000 kB\nSwapFree:   6000000 kB\n")


@pytest.fixture
def fake_proc(monkeypatch, tmp_path):
    """PROC_ROOT / CGROUP_ROOT を tmp に退避する (実 /proc を触らない)。"""
    proc = tmp_path / "proc"
    proc.mkdir()
    cgroup = tmp_path / "cgroup" / "user.slice"
    cgroup.mkdir(parents=True)
    (proc / "self").mkdir()
    monkeypatch.setattr(oom, "PROC_ROOT", str(proc))
    monkeypatch.setattr(oom, "CGROUP_ROOT", str(tmp_path / "cgroup"))
    return proc, tmp_path / "cgroup"


def write_meminfo(proc: Path, avail_bytes: int) -> None:
    (proc / "meminfo").write_text(MEMINFO.format(avail=avail_bytes // 1024), "ascii")


@pytest.fixture
def helper_calls(monkeypatch):
    """oom._run_helper を差し替えて **実 sudo を叩かない** ようにする。

    開発機 / 本番機にヘルパーが導入されていると、モックしないとテストが
    実際に root で oom_score_adj を書きに行ってしまいます。呼び出し記録を
    返しに渡すので、payload の形も検証できます。
    """
    calls: list[tuple[list[str], bytes]] = []

    def fake(cmd: list[str], payload: bytes):
        calls.append((cmd, payload))
        raise OSError("sudo is disabled in tests")

    monkeypatch.setattr(oom, "_run_helper", fake)
    return calls


@pytest.fixture(autouse=True)
def no_privileged_helper(helper_calls):
    """全テストで有効な安全装置 (ヘルパーを要求するテストは helper_calls を受ける)。"""
    return helper_calls


# ------------------------------------------------------------------- settings


def test_merged_settings_defaults():
    cfg = oom.merged_settings(None)
    assert cfg["enabled"] is True
    assert cfg["score_adj"] == oom.DEFAULT_SCORE_ADJ
    assert cfg["guard_mode"] == "block"
    assert cfg["min_available_gb"] == oom.DEFAULT_MIN_AVAILABLE_GB


def test_merged_settings_clamps_and_sanitizes():
    cfg = oom.merged_settings({"score_adj": -99999, "min_available_gb": 10_000,
                               "guard_mode": "nope", "unknown": 1, "enabled": 0})
    assert cfg["score_adj"] == -1000              # 下限でクリップ
    assert cfg["min_available_gb"] == 512.0       # 上限でクリップ
    assert cfg["guard_mode"] == "block"           # 未知の値は既定へ
    assert cfg["enabled"] is False
    assert "unknown" not in cfg
    bad = oom.merged_settings({"score_adj": "abc", "min_available_gb": None})
    assert bad["score_adj"] == oom.DEFAULT_SCORE_ADJ
    assert bad["min_available_gb"] == oom.DEFAULT_MIN_AVAILABLE_GB


# --------------------------------------------------------------- memory guard


def test_pressure_states(fake_proc):
    proc, _ = fake_proc
    cfg = oom.merged_settings({"min_available_gb": 8})
    write_meminfo(proc, 20 * GB)
    assert oom.pressure(cfg)["state"] == "ok"
    write_meminfo(proc, 6 * GB)
    assert oom.pressure(cfg)["state"] == "warn"      # 予約 8GB 未満
    write_meminfo(proc, 3 * GB)
    p = oom.pressure(cfg)
    assert p["state"] == "crit"                       # 予約の半分以下
    assert p["available_gb"] == pytest.approx(3.0, abs=0.05)
    assert p["swap_free"] == 6000000 * 1024


def test_guard_block_warn_off(fake_proc):
    proc, _ = fake_proc
    write_meminfo(proc, 3 * GB)
    blocked = oom.guard(oom.merged_settings({"guard_mode": "block", "min_available_gb": 8}))
    assert blocked["ok"] is False and "空きメモリ" in blocked["message"]
    warned = oom.guard(oom.merged_settings({"guard_mode": "warn", "min_available_gb": 8}))
    assert warned["ok"] is True and warned["message"]
    off = oom.guard(oom.merged_settings({"guard_mode": "off", "min_available_gb": 8}))
    assert off["ok"] is True and off["message"] == ""
    zero = oom.guard(oom.merged_settings({"min_available_gb": 0}))   # 予約 0 = ガード無効
    assert zero["ok"] is True and zero["mode"] == "off"


def test_guard_passes_when_plenty(fake_proc):
    proc, _ = fake_proc
    write_meminfo(proc, 40 * GB)
    g = oom.guard(oom.merged_settings({"guard_mode": "block"}))
    assert g["ok"] is True and g["message"] == "" and g["state"] == "ok"



# ------------------------------------------------------------------- /proc 系


def test_meminfo_units(fake_proc):
    proc, _ = fake_proc
    write_meminfo(proc, 12 * GB)
    mi = oom.meminfo()
    assert mi["MemAvailable"] == 12 * GB              # kB -> bytes
    assert mi["MemTotal"] == 32000000 * 1024
    assert oom.available_bytes() == 12 * GB


def test_tree_pids_walks_ppid_tree(fake_proc, monkeypatch):
    proc, _ = fake_proc

    def stat(pid: int, ppid: int) -> None:
        d = proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(f"{pid} (python3) S {ppid} {pid} 0 0\n", "ascii")

    for pid, ppid in [(100, 10), (110, 100), (111, 100), (120, 111), (999, 500)]:
        stat(pid, ppid)
    assert sorted(oom.tree_pids(100)) == [100, 110, 111, 120]   # 無関係な 999 は除外
    assert oom.tree_pids(120) == [120]
    monkeypatch.setattr(oom, "PROC_ROOT", str(proc / "nonexistent"))
    assert oom.tree_pids(7) == [7]                          # /proc 不可なら自分だけ


def test_cgroup_oom_kills(fake_proc):
    proc, cgroup = fake_proc
    (proc / "self" / "cgroup").write_text("0::/user.slice/llm-launcher.scope\n", "ascii")
    assert oom.cgroup_oom_kills() is None                   # memory.events が無い
    d = cgroup / "user.slice" / "llm-launcher.scope"
    d.mkdir(parents=True)
    (d / "memory.events").write_text("low 0\nhigh 3\nmax 1\noom 0\noom_kill 4\n", "ascii")
    assert oom.cgroup_oom_kills() == 4
    assert oom.cgroup_dir().endswith("llm-launcher.scope")


def test_death_reason():
    assert oom.death_reason(-9, 1, 2.5)[1] is True
    assert "OOM kill を検知" in oom.death_reason(137, 2)[0]
    assert "SIGKILL" in oom.death_reason(-9, None, 30.0)[0]
    assert oom.death_reason(1, 0, 30.0) == ("", False)       # 通常の終了は触れない
    assert oom.death_reason(None, 5, 30.0) == ("", False)
    assert "空き 2.5GB" in oom.death_reason(-9, 1, 2.5)[0]


# ---------------------------------------------------- 適用と権限フォールバック


def test_apply_score_reports_permission_error(monkeypatch):
    monkeypatch.setattr(oom, "write_score", lambda pid, score: (False, "permission denied"))
    monkeypatch.setattr(oom, "helper_available", lambda: False)
    monkeypatch.setattr(oom, "can_lower_score", lambda: False)
    res = oom.apply_score(1234, -700, pids=[1234, 1235])
    assert res["applied"] == 0 and res["denied"] == 2
    assert "permission denied" in res["error"] and res["helper"] is False


def test_apply_score_falls_back_to_helper(monkeypatch):
    monkeypatch.setattr(oom, "write_score", lambda pid, score: (False, "permission denied"))
    monkeypatch.setattr(oom, "helper_available", lambda: True)
    called = {}

    class R:
        returncode, stdout, stderr = 0, b"applied=2/2", b""

    def fake_helper(cmd, payload):
        called["cmd"], called["input"] = cmd, payload
        return R()

    monkeypatch.setattr(oom, "_run_helper", fake_helper)
    res = oom.apply_score(1234, -700, pids=[1234, 1235])
    assert res["via"] == "helper" and res["denied"] == 0 and res["applied"] == 2
    assert called["cmd"] == ["sudo", "-n", oom.HELPER_PATH]          # 引数は渡さない
    assert called["input"] == b"1234\n-700\n"          # stdin に bytes で渡す (str だと落ちる)


def test_apply_via_helper_missing(monkeypatch):
    monkeypatch.setattr(oom, "helper_available", lambda: False)
    assert oom.apply_via_helper(1, -700) == (False, "helper not installed")


def test_protect_self_error_is_visible(monkeypatch):
    monkeypatch.setattr(oom, "write_score", lambda pid, score: (False, "permission denied (EACCES)"))
    monkeypatch.setattr(oom, "apply_via_helper", lambda pid, score: (False, "helper not installed"))
    r = oom.protect_self(-700)
    assert r["applied"] is False and "permission denied" in r["error"]


# ------------------------------------------------------------------- status


def test_status_warns_without_privilege(fake_proc, monkeypatch):
    proc, _ = fake_proc
    write_meminfo(proc, 6 * GB)                       # 既定予約 8GB を下回る
    monkeypatch.setattr(oom, "can_lower_score", lambda: False)
    monkeypatch.setattr(oom, "helper_available", lambda: False)
    st = oom.status(oom.merged_settings({"score_adj": -700}),
                    {"applied": False, "via": "", "error": "permission denied"})
    assert st["level"] == "warn" and st["can_lower"] is False
    assert "oom-protect-install.sh" in st["verdict"] and "permission denied" in st["verdict"]
    assert st["state"] == "warn" and st["guard_mode"] == "block"


def test_status_ok_disabled_and_zero_score(fake_proc, monkeypatch):
    proc, _ = fake_proc
    write_meminfo(proc, 40 * GB)
    monkeypatch.setattr(oom, "can_lower_score", lambda: True)
    st = oom.status(oom.merged_settings({}), {"applied": True, "via": "direct", "error": ""})
    assert st["level"] == "ok" and "保護適用中" in st["verdict"]
    assert oom.status(oom.merged_settings({"enabled": False}), None)["level"] == "off"
    zero = oom.status(oom.merged_settings({"score_adj": 0}), {"applied": True, "via": "direct"})
    assert zero["level"] == "warn" and "負の値" in zero["verdict"]     # 0 のままは未保護と同等


# ------------------------------------------------- ProcessManager 起動前ガード


def make_manager(tmp_path: Path, oom_cfg: dict) -> ProcessManager:
    return ProcessManager(
        tmp_path,
        JsonStore(tmp_path / "data" / "runs.json", []),
        JsonStore(tmp_path / "data" / "profiles.json", []),
        JsonStore(tmp_path / "data" / "settings.json", {"oom": oom_cfg}),
    )


LOW_MEM = {"MemTotal": 32 * GB, "MemAvailable": 2 * GB, "SwapTotal": 0, "SwapFree": 0}


def test_start_blocked_by_memory_guard(tmp_path, monkeypatch):
    """block モードで空きが予約未満なら MemoryGuardError、force は通す。"""
    pm = make_manager(tmp_path, {"guard_mode": "block", "min_available_gb": 8})
    monkeypatch.setattr(oom, "meminfo", lambda: dict(LOW_MEM))
    monkeypatch.setattr(pm, "build_argv", lambda p: ["/bin/sleep", "30"])
    monkeypatch.setattr(process_mod, "resolve_port", lambda b, p, argv: None)
    profile = {"id": "p1", "backend": "vllm", "name": "block me"}
    with pytest.raises(MemoryGuardError, match="空きメモリ"):
        asyncio.run(pm.start(profile))
    assert pm.last_guard["state"] == "crit"
    assert not pm.sessions                              # ブロック時は履歴に残さない

    async def go() -> None:
        s = await pm.start(profile, force=True)         # force は拒否を通過させる
        assert s["status"] == "running"
        assert s["oom_kills0"] is None or isinstance(s["oom_kills0"], int)
        assert b"[oom]" in (tmp_path / s["log"]).read_bytes()
        await pm.stop(s["id"])
    asyncio.run(go())


def test_start_warn_and_off_modes_do_not_block(tmp_path, monkeypatch):
    for mode in ("warn", "off"):
        pm = make_manager(tmp_path, {"guard_mode": mode, "min_available_gb": 8})
        monkeypatch.setattr(oom, "meminfo", lambda: dict(LOW_MEM))
        monkeypatch.setattr(pm, "build_argv", lambda p: ["/bin/sleep", "30"])
        monkeypatch.setattr(process_mod, "resolve_port", lambda b, p, argv: None)

        async def go() -> None:
            s = await pm.start({"id": "p2", "backend": "vllm", "name": mode})
            assert s["status"] == "running"
            assert pm.last_guard["state"] == "crit"
            await pm.stop(s["id"])
        asyncio.run(go())


def test_apply_oom_records_permission_failure(tmp_path, monkeypatch):
    """書き込み拒否は握り潰さず、セッションとログの両方に理由を残す。"""
    pm = make_manager(tmp_path, {"score_adj": -700, "guard_mode": "off"})
    monkeypatch.setattr(oom, "write_score", lambda pid, score: (False, "permission denied (EACCES)"))
    monkeypatch.setattr(oom, "helper_available", lambda: False)
    monkeypatch.setattr(oom, "can_lower_score", lambda: False)
    monkeypatch.setattr(oom, "meminfo", lambda: dict(LOW_MEM))
    monkeypatch.setattr(pm, "build_argv", lambda p: ["/bin/sleep", "30"])
    monkeypatch.setattr(process_mod, "resolve_port", lambda b, p, argv: None)

    async def go() -> None:
        s = await pm.start({"id": "p3", "backend": "vllm", "name": "no priv"})
        assert s["oom"]["denied"] >= 1 and s["oom"]["applied"] == 0
        assert "permission denied" in s["oom"]["error"]
        text = (tmp_path / s["log"]).read_text("utf-8", "replace")
        assert "permission denied" in text and "oom-protect-install.sh" in text
        await pm.stop(s["id"])
    asyncio.run(go())


def test_apply_oom_skipped_when_disabled(tmp_path, monkeypatch):
    pm = make_manager(tmp_path, {"enabled": False, "guard_mode": "off"})
    monkeypatch.setattr(oom, "meminfo", lambda: dict(LOW_MEM))
    monkeypatch.setattr(pm, "build_argv", lambda p: ["/bin/sleep", "30"])
    monkeypatch.setattr(process_mod, "resolve_port", lambda b, p, argv: None)

    async def go() -> None:
        s = await pm.start({"id": "p4", "backend": "vllm", "name": "disabled"})
        res = pm.apply_oom(s["id"])
        assert res["skipped"] is True and res["enabled"] is False
        assert s["oom"] is None                          # オフなら触らない
        await pm.stop(s["id"])
    asyncio.run(go())


def test_check_pressure_marks_and_swift_stops(tmp_path, monkeypatch):
    """crit + swift_stop では後に起動したセッションを安全停止する。"""
    pm = make_manager(tmp_path, {"min_available_gb": 8, "swift_stop": True})
    monkeypatch.setattr(oom, "meminfo", lambda: dict(LOW_MEM))
    pm.sessions["s_old"] = {"id": "s_old", "status": "running", "pid": 4241, "started_at": 10.0}
    pm.sessions["s_new"] = {"id": "s_new", "status": "running", "pid": 4242, "started_at": 20.0}
    stopped: list[str] = []

    async def fake_stop(sid: str, grace: float = 15.0) -> None:
        stopped.append(sid)

    async def go() -> None:
        monkeypatch.setattr(pm, "stop", fake_stop)
        await pm._check_pressure()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(go())

    assert stopped == ["s_new"]
    assert pm.sessions["s_new"]["oom_swift_stop"] is True
    assert pm.sessions["s_new"]["mem_state"] == "crit"
    assert pm.sessions["s_old"]["mem_state"] == "crit"
    assert pm.sessions["s_old"].get("oom_swift_stop") is None


def test_check_pressure_skipped_without_reserve(tmp_path, monkeypatch):
    pm = make_manager(tmp_path, {"min_available_gb": 0})
    called: list[int] = []
    monkeypatch.setattr(oom, "pressure", lambda cfg: called.append(1) or {})
    pm.sessions["s1"] = {"id": "s1", "status": "running", "pid": 1, "started_at": 1.0}
    asyncio.run(pm._check_pressure())
    assert called == []


def test_oom_status_and_settings_reapply(tmp_path, monkeypatch):
    pm = make_manager(tmp_path, {"score_adj": -100})
    monkeypatch.setattr(oom, "meminfo", lambda: {"MemTotal": 32 * GB, "MemAvailable": 20 * GB,
                                                 "SwapTotal": 0, "SwapFree": 4 * GB})
    monkeypatch.setattr(oom, "write_score", lambda pid, score: (True, ""))
    st = pm.oom_status()                                 # 未適用でも形は揃う
    assert st["score_adj"] == -100 and st["guard_mode"] == "block"
    assert st["min_available_gb"] == oom.DEFAULT_MIN_AVAILABLE_GB
    assert st["verdict"] and st["install_hint"]
    applied = pm.apply_oom_settings({"score_adj": -500, "guard_mode": "warn"})
    assert applied["score_adj"] == -500 and applied["guard_mode"] == "warn"
    assert applied["self_applied"] is True and applied["self_via"] == "direct"
    assert pm.self_oom["score"] == -500
    off = pm.apply_oom_settings({"enabled": False})
    assert off["level"] == "off" and pm.self_oom["applied"] is False


# ---------------------------------------------------------------- API 配線


async def asgi_call(app, method: str, path: str, body: dict | None = None):
    """httpx なしでも動かせる最小の ASGI クライアント (TestClient の代替)。"""
    payload = b"" if body is None else json.dumps(body).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"test"), (b"content-type", b"application/json"),
                         (b"content-length", str(len(payload)).encode())],
             "client": ("127.0.0.1", 50000), "server": ("test", 80)}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 0)
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, (json.loads(raw) if raw.startswith(b"{") or raw.startswith(b"[") else raw)


def make_app(tmp_path: Path, monkeypatch, oom_cfg: dict):
    from llm_launcher import app as app_mod
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "profiles.json").write_text(json.dumps(
        [{"id": "p1", "name": "guard me", "backend": "vllm", "values": {}}]), "utf-8")
    (tmp_path / "data" / "settings.json").write_text(
        json.dumps({"backends": {}, "oom": oom_cfg}), "utf-8")
    monkeypatch.setattr(ProcessManager, "build_argv", lambda self, p: ["/bin/sleep", "30"])
    monkeypatch.setattr(process_mod, "resolve_port", lambda b, p, argv: None)
    monkeypatch.setattr(app_mod, "resolve_port", lambda b, p, argv: None)
    monkeypatch.setattr(oom, "meminfo", lambda: dict(LOW_MEM))
    return app_mod.create_app(tmp_path)


def test_api_oom_status_shape_and_guard_409(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, {"guard_mode": "block", "min_available_gb": 8})
    monkeypatch.setattr(oom, "helper_available", lambda: False)

    async def go() -> None:
        st, oom_st = await asgi_call(app, "GET", "/api/oom")
        assert st == 200
        for key in ("enabled", "score_adj", "min_available_gb", "guard_mode", "reapply",
                    "swift_stop", "state", "available", "reserve", "available_gb",
                    "can_lower", "helper", "self_score", "self_applied", "level",
                    "verdict", "oom_kills", "oomd", "install_hint"):
            assert key in oom_st, key
        assert oom_st["score_adj"] == oom.DEFAULT_SCORE_ADJ and oom_st["state"] == "crit"

        st, sysinfo = await asgi_call(app, "GET", "/api/system")
        assert st == 200 and "oom" in sysinfo and sysinfo["oom"]["guard_mode"] == "block"

        st, err = await asgi_call(app, "POST", "/api/sessions", {"profile_id": "p1"})
        assert st == 409 and "空きメモリ" in err["detail"]      # ガードは 409 で返す
        st, s = await asgi_call(app, "POST", "/api/sessions", {"profile_id": "p1", "force": True})
        assert st == 201 and s["status"] == "running"           # force は通過
        st, s = await asgi_call(app, "POST", f"/api/sessions/{s['id']}/stop")
        assert st == 200 and s["status"] == "exited"
    asyncio.run(go())


def test_api_restart_accepts_force(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, {"guard_mode": "off"})

    async def go() -> None:
        st, s = await asgi_call(app, "POST", "/api/sessions", {"profile_id": "p1"})
        assert st == 201
        st, s2 = await asgi_call(app, "POST", f"/api/sessions/{s['id']}/restart", {"force": True})
        assert st == 201 and s2["id"] != s["id"]                # body の force を受け付ける
        await asgi_call(app, "POST", f"/api/sessions/{s2['id']}/stop")
    asyncio.run(go())


def test_api_post_oom_persists_and_reapplies(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, {"guard_mode": "off"})
    written: list[tuple[int, int]] = []
    monkeypatch.setattr(oom, "write_score", lambda pid, score: written.append((pid, score)) or (True, ""))

    async def go() -> None:
        st, o = await asgi_call(app, "POST", "/api/oom",
                               {"score_adj": -300, "guard_mode": "warn", "swift_stop": True})
        assert st == 200
        assert o["score_adj"] == -300 and o["guard_mode"] == "warn" and o["swift_stop"] is True
        assert o["self_applied"] is True and o["level"] == "ok"
        saved = json.loads((tmp_path / "data" / "settings.json").read_text("utf-8"))["oom"]
        assert saved["score_adj"] == -300 and saved["guard_mode"] == "warn"
        assert saved["min_available_gb"] == oom.DEFAULT_MIN_AVAILABLE_GB   # 未指定は維持
        assert (-300) in [w[1] for w in written]                # launcher 自身へ再適用済み
        # 部分更新: 有効化を切り替えると他の値はそのまま
        st, off = await asgi_call(app, "POST", "/api/oom", {"enabled": False})
        assert st == 200 and off["level"] == "off" and off["score_adj"] == -300
    asyncio.run(go())




# ------------------------------------------------------------ 特権ヘルパー本体
# scripts/oom-protect-helper.py は root 前提だが、書式検証とツリー走査・安全条件は
# 非特権でもテストできる (実際にプロセスを生やして stdin プロトコルを確認する)。

HELPER_SCRIPT = Path(oom.__file__).resolve().parents[2] / "scripts" / "oom-protect-helper.py"
INSTALLER = HELPER_SCRIPT.parent / "oom-protect-install.sh"


def load_helper():
    spec = importlib.util.spec_from_file_location("oom_protect_helper", HELPER_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_helper_tree_only_descendants():
    """ヘルパーは root PID とその子孫だけ対象にし、親方向には遡らない。"""
    helper = load_helper()
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        pids = helper.tree(kid.pid)
        assert pids == [kid.pid]                       # 子を持たないなら自分だけ
        assert 1 not in pids and os.getppid() not in pids
    finally:
        kid.kill()
        kid.wait(timeout=5)
    assert helper.tree(999999) == [999999]             # 存在しない PID は自分のみ


@pytest.mark.parametrize("payload,rc", [("", 2), ("abc -700", 2), ("1 -700", 2),
                                        (f"{os.getpid()} -99999", 2)])
def test_helper_rejects_bad_payload(payload, rc):
    r = subprocess.run([sys.executable, str(HELPER_SCRIPT)], input=payload.encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
    assert r.returncode == rc and r.stderr


def test_helper_accepts_launcher_payload(monkeypatch):
    """apply_via_helper が送る stdin をヘルパー本体がそのまま受け付ける (書式一致)。"""
    captured: dict = {}

    class Res:
        returncode, stdout, stderr = 0, b"applied=1/1 score=-700", b""

    def fake_helper(cmd, payload):                      # 実際には sudo を起動しない
        captured["cmd"], captured["payload"] = list(cmd), payload
        return Res()

    monkeypatch.setattr(oom, "_run_helper", fake_helper)
    monkeypatch.setattr(oom, "helper_available", lambda: True)
    ok, msg = oom.apply_via_helper(os.getpid(), -700)
    assert ok and msg.startswith("applied=")
    assert captured["cmd"] == ["sudo", "-n", oom.HELPER_PATH]   # 引数なし実行だけの契約
    assert captured["payload"] == f"{os.getpid()}\n-700\n".encode()   # stdin は bytes

    r = subprocess.run([sys.executable, str(HELPER_SCRIPT)],
                       input=captured["payload"], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=15, check=False)
    err = (r.stderr or b"").decode("utf-8", "replace")
    # 書式不正 (rc=2) ではなく root ゲート (rc=1) まで到達 = プロトコル一致
    assert r.returncode in (0, 1), err


# ----------------------------------------------------- swift stop の重複防止


def test_swift_stop_does_not_refire(tmp_path, monkeypatch):
    pm = make_manager(tmp_path, {"min_available_gb": 8, "swift_stop": True})
    monkeypatch.setattr(oom, "meminfo", lambda: dict(LOW_MEM))
    pm.sessions["s1"] = {"id": "s1", "status": "running", "pid": 4241,
                         "started_at": 10.0, "oom_swift_stop": True}
    calls: list[str] = []

    async def fake_stop(sid: str, grace: float = 15.0) -> None:
        calls.append(sid)

    async def go() -> None:
        monkeypatch.setattr(pm, "stop", fake_stop)
        await pm._check_pressure()
        await asyncio.sleep(0)
    asyncio.run(go())

    assert calls == []                                      # 処理事中のセッションを二重停止しない
    assert pm.sessions["s1"]["mem_state"] == "crit"
    assert pm.sessions["s1"]["mem_available_gb"] == pytest.approx(2.0, abs=0.05)


# --------------------------------------------------------- インストーラの安全装置


def test_installer_validates_sudoers_before_writing():
    """sudoers は壊すと sudo 全体が誤作動するので、生成 → visudo 検証 → 書込み の順。

    昔の版は requiretty 未知設定入りで無検証に書き込んでおり、sudo-rs では
    /etc/sudoers.d/llm-launcher-oom-protect が毎回パースエラーになっていました。
    """
    text = INSTALLER.read_text("utf-8")
    assert "supports_requiretty" in text                          # 未知の Defaults 行を出さない
    assert 'visudo -cf "$TMP"' in text                            # 書き込み前に検証
    assert 'install -m 0440 "$TMP" "$SUDOERS"' in text
    assert text.index('visudo -cf "$TMP"') < text.index('install -m 0440 "$TMP" "$SUDOERS"')

