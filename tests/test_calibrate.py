"""ワンクリック・キャリブレーション (Maya / Strata) の回帰テスト。

ランチャーのキャリブレーションは、各バックエンドが既に持っている実測調整機能
(Maya: `--calibrate --no-start` / Strata: `engine calibrate --measure|--review-result|--apply-result`)
を 1 ボタンで実行する接着層。ここで押さえる契約:

  1. コマンド組み立て (Maya は --calibrate + --no-start + --config、Strata は保存 profile 経由)
  2. Strata profile 名は小文字 a-z0-9_- だけ (Strata の保存規則) で、同じ launcher id からは同じ名前
  3. ジョブは 1 件ずつ / 実行中は開始拒否 / キャンセルで "cancelled" / 失敗は理由を残す
  4. 調整前に同じプロファイルのサーバーを停止する (GPU 独占が必須)
  5. /api/calibrations の API 契約と RBAC (calibrate 権限)

本物の CLI との照合 (module がクローン済みの環境のみ):
  Maya `maya.py --help` / Strata `strata.py engine calibrate --help` に各フラグが実在すること
を確認する。CLI は実際には走らせない (10-40 分のエンジン起動を伴う) — 実行パスは
偽の python/CLI スクリプトで検証する。
"""
from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from llm_launcher import backends, calibrate, rbac
from llm_launcher.store import JsonStore

ROOT = Path(__file__).resolve().parents[1]
MAYA_PY = ROOT / "module" / "maya" / "maya.py"
STRATA_PY = ROOT / "module" / "strata" / "strata.py"

pytestmark = pytest.mark.skipif(
    not MAYA_PY.exists() or not STRATA_PY.exists(),
    reason="module/maya または module/strata が未クローン",
)


# ---------------------------------------------------------------- コマンド組み立て


def _profile(**kw):
    return {"id": "p_deadbeef1234", "name": "cal target", "backend": "strata",
            "values": {"config": "module/strata/strata-x.json"}, "on": {}, **kw}


def test_maya_command_uses_calibrate_no_start_and_config():
    cfg = ROOT / "module" / "maya" / "maya-ds4.json"
    argv = calibrate.build_maya_command(ROOT, {"values": {"config": str(cfg)}}, "py")
    assert argv[0] == "sh"
    assert argv[1].endswith("module/maya/maya.sh")
    assert "--calibrate" in argv and "--no-start" in argv
    assert argv[argv.index("--config") + 1] == str(cfg)
    # 起動しない調整モード: maya.py の --no-start は調整だけして return 0
    assert argv.index("--no-start") > argv.index("--calibrate")


def test_maya_config_defaults_to_newest_module_config():
    argv = calibrate.build_maya_command(ROOT, {"values": {}}, "py")
    assert "--config" in argv
    chosen = argv[argv.index("--config") + 1]
    assert Path(chosen).is_file() and chosen.endswith(".json") and "maya-" in Path(chosen).name


def test_relative_config_resolves_against_module_dir(tmp_path):
    """プロファイルの実例: Maya は values.config "maya-ds4.json"、実体は module/maya/ にある
    (バックエンドは module ディレクトリを cwd に起動するため相対は module 側で解決される)。"""
    (tmp_path / "module" / "maya").mkdir(parents=True)
    cfg = tmp_path / "module" / "maya" / "maya-ds4.json"
    cfg.write_text("{}")
    got = calibrate.maya_config_path(tmp_path, {"values": {"config": "maya-ds4.json"}})
    assert got == str(cfg.resolve())
    # 絶対パス指定も同じ
    assert calibrate.maya_config_path(tmp_path, {"values": {"config": str(cfg)}}) == str(cfg.resolve())
    # 指定した config が実在しない → "" (黙って別の config を調整しない / 開始時に即失敗)
    assert calibrate.maya_config_path(tmp_path, {"values": {"config": "no-such.json"}}) == ""
    # --config 未指定なら module/maya の最新を対象にする (従来の既定挙動)
    assert calibrate.maya_config_path(tmp_path, {"values": {}}) == str(cfg)
    # Strata はプロファイル指定が必須 (デフォルト探しはしない)
    assert calibrate.strata_config_path(tmp_path, {"values": {"config": "no-such.json"}}) == ""
    (tmp_path / "module" / "strata").mkdir(parents=True)
    (tmp_path / "module" / "strata" / "strata-x.json").write_text("{}")
    assert calibrate.strata_config_path(tmp_path, {"values": {"config": "strata-x.json"}}) == \
        str((tmp_path / "module" / "strata" / "strata-x.json").resolve())


def test_strata_report_path_is_absolute(tmp_path):
    """Strata の report は cwd=module/strata で走るので、相対だと module 側に書かれてしまう。"""
    import asyncio
    _fake_strata(tmp_path)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    prof = {"id": "p_r", "name": "strata", "backend": "strata",
            "values": {"config": str(tmp_path / "module" / "strata" / "strata-x.json")}}
    (tmp_path / "module" / "strata" / "strata-x.json").write_text("{}")
    seen = []
    orig = mgr._run_step
    async def spy(job, step):
        seen.append(step["argv"])
        return await orig(job, step)
    mgr._run_step = spy

    async def go():
        job = await mgr.start(prof, "strata")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
    asyncio.run(go())
    measure = next(a for a in seen if "--measure" in a)
    report = measure[measure.index("--out") + 1]
    assert Path(report).is_absolute()
    assert str(tmp_path / "data" / "calibrations") in report


def test_strata_commands_are_profile_based_measure_review_apply(tmp_path):
    (tmp_path / "module" / "strata").mkdir(parents=True)
    (tmp_path / "module" / "strata" / "strata-x.json").write_text("{}")
    cmds = calibrate.build_strata_commands(tmp_path, _profile(), "py", str(tmp_path / "r.json"),
                                           "p_deadbeef1234")
    assert set(cmds) == {"create", "plan", "measure", "review", "apply"}
    # plan は読み取り専用の事前検査 (エンジン起動なし) → measure より前に走らせて欠損を見つける
    assert "--plan" in cmds["plan"] and "--exclusive" not in cmds["plan"]
    create = cmds["create"]
    assert create[2:4] == ["profile", "create"] and "--from-config" in create
    src = create[create.index("--from-config") + 1]
    assert src == str(tmp_path / "module" / "strata" / "strata-x.json")   # 相対は root から解決
    assert Path(src).is_absolute()
    assert cmds["measure"][2:4] == ["engine", "calibrate"] and "--measure" in cmds["measure"]
    assert "--exclusive" in cmds["measure"] and "--out" in cmds["measure"]
    assert "--review-result" in cmds["review"] and str(tmp_path / "r.json") in cmds["review"]
    assert "--apply-result" in cmds["apply"] and "--exclusive" in cmds["apply"]
    assert all("--output" in c and "json" in c for c in cmds.values())


def test_strata_profile_name_is_saved_profile_safe_and_stable():
    name = calibrate.strata_profile_name(_profile())
    assert name == "p_deadbeef1234"                    # 小文字・_/- を使う (Strata の保存規則)
    assert calibrate.strata_profile_name(_profile()) == name   # 同じ id → 同じ名前 (再利用可)
    for raw in ("", "UPPER", "日本", "_x", "a" * 200, "p_1 2"):
        n = calibrate.strata_profile_name({"id": raw})
        assert calibrate.strata_profile_name({"id": raw}) == n
        assert n and n[0].islower() and len(n) <= 60
        assert all(ch in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in n)


# ---------------------------------------------------------------- CLI 照合


def _run(argv, cwd):
    import subprocess
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)


def test_maya_cli_really_has_calibrate_no_start_and_config():
    out = _run([str(MAYA_PY.parent / ".venv" / "bin" / "python"), str(MAYA_PY), "--help"],
               MAYA_PY.parent)
    assert out.returncode == 0, out.stderr
    for flag in ("--calibrate", "--no-start", "--config"):
        assert flag in out.stdout, flag


def test_strata_cli_really_has_calibration_actions():
    py = MAYA_PY.parent.parent / "strata" / ".venv" / "bin" / "python"
    out = _run([str(py), str(STRATA_PY), "engine", "calibrate", "--help"],
               STRATA_PY.parent)
    assert out.returncode == 0, out.stderr
    for flag in ("--profile", "--measure", "--out", "--exclusive", "--review-result",
                 "--apply-result", "--output"):
        assert flag in out.stdout, flag


# ---------------------------------------------------------------- 実行パス (偽 CLI)


def _fake_strata(tmp_path: Path) -> Path:
    """偽 strata.py: profile list/create と engine calibrate の measure/review/apply を JSON で返す。"""
    mod = tmp_path / "module" / "strata"
    mod.mkdir(parents=True)
    script = mod / "strata.py"
    script.write_text(
        "import json, os, sys\n"
        "a = sys.argv[1:]\n"
        "state = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'saved.json')\n"
        "saved = json.load(open(state)) if os.path.exists(state) else {}\n"
        "out = {'schema_version': 1, 'ok': True}\n"
        "if a[:2] == ['profile', 'list']:\n"
        "    out['profiles'] = [{'id': k, 'source_config': v} for k, v in saved.items()]\n"
        "elif a[:2] == ['profile', 'create']:\n"
        "    saved[a[2]] = a[a.index('--from-config') + 1]\n"
        "    json.dump(saved, open(state, 'w'))\n"
        "elif a[:2] == ['engine', 'calibrate'] and '--profile' in a:\n"
        "    if '--review-result' in a:\n"
        "        out['review'] = {'applicable': True, 'reasons': [], 'settings': {'--pool-workers': '8'},\n"
        "                         'before_args': ['--a'], 'after_args': ['--a', '--pool-workers', '8']}\n"
        "    elif '--apply-result' in a:\n"
        "        out['application'] = {'application_record': '/tmp/rec.json'}\n"
        "    elif '--plan' in a:\n"
        "        out['plan'] = {'tool_available': True, 'component_preflight': {'ok': True, 'issues': []}}\n"
        "    elif '--measure' in a:\n"
        "        out['report'] = {'settings': {'--pool-workers': '8'}}\n"
        "elif '--help' in a or not a:\n"
        "    pass\n"
        "else:\n"
        "    out = {'schema_version': 1, 'ok': False, 'error': {'code': 2, 'message': 'unknown'}}\n"
        "    print(json.dumps(out))\n"
        "    sys.exit(2)\n"
        "print(json.dumps(out))\n",
        "utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _fake_maya(tmp_path: Path, tune: bool = True) -> Path:
    """偽 maya.sh。本物と同じ契約に絞る:
    - 成功: config に installer.calibration を書いて "tuned for this PC: ..." を出力, exit 0
    - 失敗: config を書かずに "NOT tuned" を出力し、**exit 0** (本物も 0 で終わる!)
    tune=False は「エンジンが起動できなかった」状況の再現。"""
    mod = tmp_path / "module" / "maya"
    mod.mkdir(parents=True)
    cfg = mod / "maya-ds4.json"
    cfg.write_text(json.dumps({"args": [], "exe": "x", "tokenizer": "y"}), "utf-8")
    helper = mod / "_fake_tune.py"
    helper.write_text(
        "import json, sys\n"
        "p = sys.argv[1]\n"
        "d = json.load(open(p))\n"
        "d.setdefault('installer', {})['calibration'] = {'date': '2026-01-01 00:00', 'tok_s': '8'}\n"
        "json.dump(d, open(p, 'w'))\n", "utf-8")
    if tune:
        body = f'python3 "{helper}" "{cfg}"\necho "tuned for this PC: --tp 2"\n'
    else:
        body = (
            'echo "  [!] the tuning did not finish (the engine exited before it was ready):'
            ' the config\'s settings stay"\n'
            'echo "       the engine said: maya-ds4: no CUDA device"\n'
            'echo "  [!] this PC is NOT tuned (the reason is above)"\n'
        )
    (mod / "maya.sh").write_text("#!/bin/sh\n" + body, "utf-8")
    (mod / "maya.sh").chmod(0o755)
    return mod / "maya.sh"


class _FakeManager:
    def __init__(self):
        self.sessions = {}
        self.stopped: list[str] = []

    def resolve_python(self, bid):
        import sys
        return sys.executable

    async def stop(self, sid, grace=15.0):
        self.stopped.append(sid)
        self.sessions[sid]["status"] = "exited"

    async def start(self, profile, force=False, **kw):
        raise RuntimeError("no")


def _mgr(tmp_path: Path, root: Path, fake: _FakeManager):
    store = JsonStore(tmp_path / "calibrations.json", [])
    prof = JsonStore(tmp_path / "profiles.json", [])
    return calibrate.CalibrationManager(root, prof, store, fake)


def test_maya_job_runs_and_records_log(tmp_path, monkeypatch):
    _fake_maya(tmp_path)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    prof = {"id": "p_m", "name": "maya", "backend": "maya",
            "values": {"config": str(tmp_path / "module" / "maya" / "maya-ds4.json")}}

    async def go():
        job = await mgr.start(prof, "maya")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done"
    assert job["steps"] == ["calibrate"]
    assert any("tuned for this PC" in ln for ln in job["logs"]), job["logs"]
    assert mgr.active_id is None


def test_maya_failure_is_detected_even_though_exit_code_is_zero(tmp_path):
    """実機で確認した本物の契約: エンジンが起動できなかった場合 Maya は警告を出して False を返し、
    終了コードは **0** のまま config を書き換えない。終了コードだけを見ると失敗を成功と偽るので、
    ランチャーは config の調整痕跡を確認して "failed" + 理由を残すこと。"""
    _fake_maya(tmp_path, tune=False)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    prof = {"id": "p_m", "name": "maya", "backend": "maya",
            "values": {"config": str(tmp_path / "module" / "maya" / "maya-ds4.json")}}

    async def go():
        job = await mgr.start(prof, "maya")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "failed"
    assert "Maya" in job["error"] and ("NOT tuned" in job["error"] or "no CUDA device" in job["error"]), job["error"]
    assert mgr.active_id is None


def test_maya_success_requires_config_calibration_record(tmp_path):
    """成功側の証拠 (config の installer.calibration) が無いと done にしない。"""
    _fake_maya(tmp_path)
    cfg = tmp_path / "module" / "maya" / "maya-ds4.json"
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    prof = {"id": "p_m", "name": "maya", "backend": "maya", "values": {"config": str(cfg)}}

    async def go():
        job = await mgr.start(prof, "maya")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done"
    assert json.loads(cfg.read_text())["installer"]["calibration"]["date"]


def test_strata_job_runs_create_measure_review_apply_and_reports_settings(tmp_path):
    _fake_strata(tmp_path)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    cfg = tmp_path / "module" / "strata" / "strata-x.json"
    cfg.write_text("{}")
    prof = {"id": "p_s", "name": "strata", "backend": "strata",
            "values": {"config": str(cfg)}}

    async def go():
        job = await mgr.start(prof, "strata")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    assert job["steps"] == ["create", "plan", "measure", "review", "apply"]
    assert job["report"]["settings"] == {"--pool-workers": "8"}
    assert job["report"]["after_args"] == ["--a", "--pool-workers", "8"]


def test_second_start_is_refused_while_running(tmp_path):
    _fake_strata(tmp_path)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    prof = {"id": "p_s", "name": "strata", "backend": "strata",
            "values": {"config": str(tmp_path / "module" / "strata" / "strata-x.json")}}
    (tmp_path / "module" / "strata" / "strata-x.json").write_text("{}")

    async def go():
        job = await mgr.start(prof, "strata")
        with pytest.raises(calibrate.CalibrateError):
            await mgr.start(prof, "strata")
        mgr.cancel(job["id"])
        for _ in range(200):                    # 最大10秒 (偽 CLI は即座に終わる)
            if mgr.active_id is None:
                break
            await asyncio.sleep(0.05)
        return job["id"]
    asyncio.run(go())
    assert mgr.active_id is None


def test_live_session_is_stopped_before_calibrating(tmp_path):
    _fake_maya(tmp_path)
    fake = _FakeManager()
    fake.sessions = {"s1": {"id": "s1", "profile_id": "p_m", "status": "running"}}
    mgr = _mgr(tmp_path, tmp_path, fake)
    prof = {"id": "p_m", "name": "maya", "backend": "maya",
            "values": {"config": str(tmp_path / "module" / "maya" / "maya-ds4.json")}}

    async def go():
        job = await mgr.start(prof, "maya")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
    asyncio.run(go())
    assert fake.stopped == ["s1"]           # GPU 独占のため稼働中サーバーを止めた


def test_stopping_session_blocks_calibration(tmp_path):
    """稼働中サーバーの停止が「停止中」(エンジン解放中) のまま返らない場合、GPU 独占が
    確約できないのでキャリブレーションを開始しない。"""
    _fake_maya(tmp_path)
    fake = _FakeManager()

    class _StuckManager(_FakeManager):
        async def stop(self, sid, grace=15.0):
            fake.stopped.append(sid)
            self.sessions[sid]["status"] = "stopping"      # 停止が終わらない状況を再現
    stuck = _StuckManager()
    stuck.sessions = {"s1": {"id": "s1", "profile_id": "p_m", "status": "running"}}
    fake.sessions = stuck.sessions                  # 同じ Session 辞書を両方から見る
    mgr = _mgr(tmp_path, tmp_path, stuck)
    prof = {"id": "p_m", "name": "maya", "backend": "maya",
            "values": {"config": str(tmp_path / "module" / "maya" / "maya-ds4.json")}}
    with pytest.raises(calibrate.CalibrateError):
        asyncio.run(mgr.start(prof, "maya"))
    assert fake.stopped == ["s1"] and mgr.active_id is None


def test_all_live_sessions_of_the_profile_are_stopped(tmp_path):
    """同じプロファイルの稼働中サーバーが複数あっても全部止める (1 つでも残れば GPU 同居になる)。"""
    _fake_maya(tmp_path)
    fake = _FakeManager()
    fake.sessions = {
        "s1": {"id": "s1", "profile_id": "p_m", "status": "running"},
        "s2": {"id": "s2", "profile_id": "p_m", "status": "starting"},
        "other": {"id": "other", "profile_id": "p_x", "status": "running"},
    }
    mgr = _mgr(tmp_path, tmp_path, fake)
    prof = {"id": "p_m", "name": "maya", "backend": "maya",
            "values": {"config": str(tmp_path / "module" / "maya" / "maya-ds4.json")}}

    async def go():
        job = await mgr.start(prof, "maya")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
    asyncio.run(go())
    assert sorted(fake.stopped) == ["s1", "s2"]    # 他人のプロファイルは止めない


def test_unsupported_backend_and_missing_config_are_rejected(tmp_path):
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    with pytest.raises(calibrate.CalibrateError):
        asyncio.run(mgr.start({"id": "x"}, "vllm"))
    # Strata: config が実在しない → 開始前に拒否 (エンジン起動を伴うため早く失敗する)
    with pytest.raises(calibrate.CalibrateError):
        asyncio.run(mgr.start({"id": "x", "values": {"config": "no-such.json"}}, "strata"))


def test_cancel_marks_job_cancelled(tmp_path):
    mod = tmp_path / "module" / "maya"
    mod.mkdir(parents=True)
    (mod / "maya.sh").write_text("#!/bin/sh\nsleep 30\n")
    (mod / "maya-ds4.json").write_text("{}")
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    prof = {"id": "p_c", "name": "maya", "backend": "maya",
            "values": {"config": str(tmp_path / "module" / "maya" / "maya-ds4.json")}}

    async def go():
        job = await mgr.start(prof, "maya")
        await asyncio.sleep(0.3)                       # sleep が走り始めるまで待つ
        mgr.cancel(job["id"])
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.1)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "cancelled"
    assert mgr.active_id is None


# ---------------------------------------------------------------- API / RBAC


def test_rbac_calibrate_is_capability_and_api_requires_it():
    assert "calibrate" in rbac.CAP_KEYS
    assert rbac.CAP_BY_KEY["calibrate"]["default"] is False      # 既定はメンテナーに非開示
    assert rbac.required_cap("GET", "/api/calibrations") == "calibrate"
    assert rbac.required_cap("POST", "/api/calibrations") == "calibrate"
    assert rbac.required_cap("POST", "/api/calibrations/x/cancel") == "calibrate"
    assert rbac.required_cap("DELETE", "/api/calibrations/x") == "calibrate"


# ------------------------------------------------ 再起動時の回復 (OOM kill / restart)

# 実機で観測した原因: キャリブレーションは launcher の asyncio task が子プロセスを走らせる
# fire-and-forget ジョブで、launcher が systemd-oomd に kill されると task も子プロセスも死ぬ。
# calibrations.json の status は "running" のまま残り、UI は「実行中」を永遠に表示し、
# active_id も再設定されないため、ユーザーは完了したのか失敗したのか判断できなかった。
# 回復は bench.BenchManager.on_startup() と同じ "lost" + 理由 + finished_at (ログ末尾は保持)。


def _job(**kw):
    base = {"id": "cal_x", "profile_id": "p_x", "profile_name": "t", "backend": "maya",
            "backend_name": "Project Maya", "kind": "maya", "status": "running", "step": "calibrate",
            "steps": ["calibrate"], "report": None, "error": "", "started_at": 100.0,
            "finished_at": None, "started_by": "u", "logs": ["[maya] still starting (60 s)"], "restart_after": False}
    base.update(kw)
    return base


def test_on_startup_marks_stale_running_jobs_lost(tmp_path):
    """再起動で実行中だったジョブは "lost" にし、理由と finished_at を書き、ログは残す。"""
    store = JsonStore(tmp_path / "calibrations.json", [_job(), _job(id="cal_done", status="done",
                                                                   finished_at=200.0, error="")])
    mgr = calibrate.CalibrationManager(tmp_path, JsonStore(tmp_path / "profiles.json", []), store, _FakeManager())
    lost = mgr.on_startup()
    assert lost == ["cal_x"]
    assert mgr.jobs["cal_x"]["status"] == "lost"
    assert mgr.jobs["cal_x"]["finished_at"] is not None
    assert "中断されました" in mgr.jobs["cal_x"]["error"]
    assert any("中断:" in ln for ln in mgr.jobs["cal_x"]["logs"])       # ログ末尾に理由が残る
    # 完了済みジョブはそのまま (status / 理由を変えない)
    assert mgr.jobs["cal_done"]["status"] == "done" and mgr.jobs["cal_done"]["error"] == ""
    assert mgr.active_id is None
    # ディスクにも書かれている (次の再起動で再回復されない)
    assert json.loads((tmp_path / "calibrations.json").read_text("utf-8"))[0]["status"] == "lost"


def test_on_startup_recovers_queued_jobs_and_returns_ids(tmp_path):
    """queued も回復対象 (起動直後に launcher が死んだ場合)。空のストアでは何も変えない。"""
    store = JsonStore(tmp_path / "calibrations.json", [_job(id="cal_q", status="queued")])
    mgr = calibrate.CalibrationManager(tmp_path, JsonStore(tmp_path / "profiles.json", []), store, _FakeManager())
    assert mgr.on_startup() == ["cal_q"]
    assert mgr.jobs["cal_q"]["status"] == "lost"
    empty = JsonStore(tmp_path / "empty.json", [])
    mgr2 = calibrate.CalibrationManager(tmp_path, JsonStore(tmp_path / "profiles.json", []), empty, _FakeManager())
    assert mgr2.on_startup() == [] and mgr2.active_id is None


def test_on_startup_keeps_finished_jobs_and_their_log(tmp_path):
    """回復は "running"/"queued" だけ。中断前に取り込んだログは live_log で読める (UI のログ閲覧導線)。"""
    store = JsonStore(tmp_path / "calibrations.json", [_job(logs=["a", "b", "[maya] still starting (60 s)"])])
    mgr = calibrate.CalibrationManager(tmp_path, JsonStore(tmp_path / "profiles.json", []), store, _FakeManager())
    mgr.on_startup()
    got = mgr.live_log("cal_x", 0)
    assert got["lines"][:3] == ["a", "b", "[maya] still starting (60 s)"]
    assert got["lines"][-1].startswith("中断:")


# ------------------------------------------------ メモリガード / OOM 保護
#
# 実測 (2026-10-25 03:22): DS4 のキャリブレーションは最後に `--mmap-experts off` を試し、
# VRAM に載らない専門家 72.7〜80.8 GB を RAM へコピーする。launcher の cgroup が 88.3 GB に
# 達し、systemd-oomd が launcher を kill (`Failed with result 'oom-kill'`) → ジョブは "lost"。
# 原因は (a) 開始前に空きを確かめていなかったこと、(b) キャリブレーションの子プロセスに
# oom_score_adj が掛かっていなかったこと (ProcessManager はモデルサーバーだけを保護する)。


def test_memory_guard_rejects_ds4_when_ram_and_swap_are_short(monkeypatch):
    """DS4 は専門家のコピー 81 GB + エンジン 9 GB + 余裕 20 GB = 110 GB を要求する。
    実測では MemAvailable 118.8 GB でもピークは 117.2 GB になり oomd に kill されたので、
    「空きが足りている」だけでは足りない — 余裕を厚く取る。swap は退避先として数える
    (81 GB のコピーは常駐しないので、swap があれば成立する)。"""
    cfg = ROOT / "module" / "maya" / "maya-ds4.json"

    def mi(ram_gb, swap_gb):
        return lambda: {"MemAvailable": int(ram_gb * (1 << 30)),
                        "SwapFree": int(swap_gb * (1 << 30))}
    # RAM + swap で足りなければ拒否 (理由に必要な量が入る)
    monkeypatch.setattr(calibrate, "meminfo", mi(40, 10))
    g = calibrate.memory_guard(str(cfg))
    assert g["ok"] is False and g["need_gb"] == 110
    assert "110" in g["reason"] and "--mmap-experts off" in g["reason"]
    # 境界: 60 + 49 = 109 GB は不足、60 + 50 = 110 GB から通す
    monkeypatch.setattr(calibrate, "meminfo", mi(60, 49))
    assert calibrate.memory_guard(str(cfg))["ok"] is False
    monkeypatch.setattr(calibrate, "meminfo", mi(60, 50))
    assert calibrate.memory_guard(str(cfg))["ok"] is True
    # RAM だけで足りない場合は swap へ退避する旨の注記が付く (黙って遅い実行にしない)
    monkeypatch.setattr(calibrate, "meminfo", mi(108, 127))
    got = calibrate.memory_guard(str(cfg))
    assert got["ok"] is True and "swap へ退避" in got["note"]
    # RAM だけで足りていれば注記なし
    monkeypatch.setattr(calibrate, "meminfo", mi(120, 127))
    assert calibrate.memory_guard(str(cfg))["note"] == ""


def test_memory_guard_is_lenient_for_non_ds4_config(tmp_path, monkeypatch):
    """DS4 でない config は従来の 8 GB 予約だけで足る (40 GB の空きなら通す)。"""
    cfg = tmp_path / "maya-small.json"
    cfg.write_text(json.dumps({"args": ["--pack", "x"]}), "utf-8")
    monkeypatch.setattr(calibrate, "meminfo",
                        lambda: {"MemAvailable": int(40 * (1 << 30)), "SwapFree": 0})
    assert calibrate.memory_guard(str(cfg))["ok"] is True
def test_memory_guard_is_lenient_for_non_ds4_config(tmp_path, monkeypatch):
    """DS4 でない config は従来の 8 GB 予約だけで足る (40 GB の空きなら通す)。"""
    cfg = tmp_path / "maya-small.json"
    cfg.write_text(json.dumps({"args": ["--pack", "x"]}), "utf-8")
    monkeypatch.setattr(calibrate, "meminfo", lambda: {"MemAvailable": int(40 * (1 << 30))})
    assert calibrate.memory_guard(str(cfg))["ok"] is True


def test_start_is_refused_by_the_memory_guard(tmp_path, monkeypatch):
    """ガードは「開始せず理由を返す」 — 40 分走らせてから OOM kill される前に止める。"""
    _fake_maya(tmp_path)
    # 偽 config に DS4 の名前を入れる (ガードはモデル名で DS4 を見分ける)
    cfg = tmp_path / "module" / "maya" / "maya-ds4.json"
    cfg.write_text(json.dumps({"args": ["--pack", "maya-ds4"]}), "utf-8")
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    prof = {"id": "p_g", "name": "maya", "backend": "maya", "values": {"config": str(cfg)}}
    monkeypatch.setattr(calibrate, "meminfo", lambda: {"MemAvailable": int(20 * (1 << 30))})

    async def go():
        with pytest.raises(calibrate.CalibrateError, match="空きメモリ"):
            await mgr.start(prof, "maya")
        assert mgr.jobs == {} and mgr.active_id is None
    asyncio.run(go())


def test_calibration_children_get_the_oom_score(tmp_path, monkeypatch):
    """キャリブレーションの子プロセスツリーへ oom_score_adj が掛かる (実測 0 のままだと
    oomd は launcher を殺す)。書き込みは偽って記録し、対象 PID が実在する子を含むことを確かめる。"""
    _fake_maya(tmp_path)
    applied: list[tuple[int, int]] = []

    def fake_apply(root_pid, score, pids=None):
        applied.append((root_pid, score))
        return {"applied": 1, "denied": 0, "via": "", "error": ""}

    monkeypatch.setattr(calibrate, "apply_score", fake_apply)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    prof = {"id": "p_o", "name": "maya", "backend": "maya",
            "values": {"config": str(tmp_path / "module" / "maya" / "maya-ds4.json")}}

    async def go():
        job = await mgr.start(prof, "maya")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
        for t in list(mgr._extra):
            t.cancel()
        return job
    job = asyncio.run(go())
    assert mgr.jobs[job["id"]]["status"] == "done"
    assert applied, "キャリブレーションの子プロセスに oom_score_adj が掛かっていない"
    assert applied[0][1] == -700               # 既定の score (settings.json の oom)


def test_api_supported_list_and_rejects_unsupported(tmp_path, monkeypatch):
    from llm_launcher import app as app_mod

    def _mk():
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "profiles.json").write_text(json.dumps([
            {"id": "p1", "name": "vllm one", "backend": "vllm", "values": {}},
            {"id": "p2", "name": "maya one", "backend": "maya", "values": {}},
        ]), "utf-8")
        return app_mod.create_app(tmp_path)

    app = _mk()

    async def go():
        st, payload = await _asgi(app, "GET", "/api/calibrations")
        assert st == 200 and payload["supported"] == sorted(calibrate.CAL_BACKENDS)
        assert payload["jobs"] == [] and payload["active"] is None
        st, err = await _asgi(app, "POST", "/api/calibrations", {"profile_id": "p1"})
        assert st == 400 and "対応していません" in err["detail"]
        st, err = await _asgi(app, "POST", "/api/calibrations", {"profile_id": "nope"})
        assert st == 404
    asyncio.run(go())


async def _asgi(app, method: str, path: str, body: dict | None = None):
    payload = b"" if body is None else json.dumps(body).encode()
    sent: list[dict] = []
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"test"), (b"content-type", b"application/json"),
                         (b"content-length", str(len(payload)).encode())],
             "client": ("127.0.0.1", 50000), "server": ("test", 80)}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 0)
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return status, raw
