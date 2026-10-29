"""ワンクリック・キャリブレーション (Maya / Strata / FreeToken) の回帰テスト。

ランチャーのキャリブレーションは、各バックエンドが既に持っている実測調整機能
(Maya: `--calibrate --no-start` / Strata: `engine calibrate --measure|--review-result|--apply-result`)
を 1 ボタンで実行する接着層。FreeToken だけ CLI が無く、launcher 内でエンジンを
再起動しながら MTP ゲートを実測する (calibrate._run_freetoken)。ここで押さえる契約:

  1. コマンド組み立て (Maya は --calibrate + --no-start + --config、Strata は保存 profile 経由)
  2. Strata profile 名は小文字 a-z0-9_- だけ (Strata の保存規則) で、同じ launcher id からは同じ名前
  3. ジョブは 1 件ずつ / 実行中は開始拒否 / キャンセルで "cancelled" / 失敗は理由を残す
  4. 調整前に同じプロファイルのサーバーを停止する (GPU 独占が必須)
  5. /api/calibrations の API 契約と RBAC (calibrate 権限)
  6. FreeToken: 座標降下の採用規則 (+2% 超だけ) / 勝者を profile env へ書き戻し /
     基準セル失敗は job 失敗・候補セル失敗はスキップ継続 / LATENCY 計測 env は書かない

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

    def build_argv(self, profile):
        import sys
        v = profile.get("values") or {}
        return [sys.executable, "-m", "freetoken", "--port", str(v.get("port") or "1919"),
                "--speculative", str(v.get("speculative") or "none")]


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


# ---------------------------------------------------------------- FreeToken (in-process sweep)
#
# FreeToken 用キャリブレータは CLI が無く、launcher が ProcessManager 経由でエンジンを
# 再起動しながら greedy プローブを回す。ここでは本物のエンジンを起動せず、
# _ft_run_cell (1 セル = 起動→計測→停止) を偽って「座標降下の採用規則」と
# 「env 書き戻し」と「失敗・中断の扱い」だけ押さえる。


FT_ALL = list(calibrate.FREETOKEN_MTP_DEFAULTS)


def _seed_profiles(tmp_path, prof):
    import json as _json
    (tmp_path / "profiles.json").write_text(_json.dumps([prof]), "utf-8")


def _ft_scores(base=10.0, wins=None, fail=()):
    """ラベル → score の台本。fail に載ったラベルは ok:False を返す。"""
    seen = []

    def patch(mgr):
        async def fake_cell(job, profile, base_env, label, overrides, copy=False, spec=None):
            seen.append((label, dict(overrides)))
            getattr(mgr, "ft_copies", mgr.__dict__.setdefault("ft_copies", [])).append(copy)
            if label in fail:
                return {"ok": False, "error": "boom", "latency": ""}
            return {"ok": True, "score": (wins or {}).get(label, base),
                    "tokens": 128, "secs": round(128 / (wins or {}).get(label, base), 2),
                    "latency": "[latency] win=5.0s loops=100 drain 61% (n=100 mean=8.0 p95=12.0ms)"}
        mgr._ft_run_cell = fake_cell
        return seen
    return patch


def _run_ft_job(mgr, prof):
    async def go():
        job = await mgr.start(prof, "freetoken")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[job["id"]]
    return asyncio.run(go())


def test_freetoken_kind_is_supported_in_every_entry_point():
    assert calibrate.CAL_BACKENDS["freetoken"] == "freetoken"
    assert calibrate.CAL_BACKENDS["freetoken-fork"] == "freetoken"


def test_ft_short_and_candidates(tmp_path):
    assert calibrate.ft_short("FREETOKEN_MTP_REJECT_STREAK") == "reject_streak"
    # 現在値と同じ候補は打たない (基準の繰り返しを省く)
    assert calibrate.ft_candidates("FREETOKEN_MTP_MIN_P",
                                   {"FREETOKEN_MTP_MIN_P": "0.3"}) == ["0.5"]
    # 現在値が既定 (0) なら両方打つ
    assert calibrate.ft_candidates("FREETOKEN_MTP_MIN_P", {}) == ["0.3", "0.5"]
    # 採用後の winner 値が grid 値の場合も除外
    assert calibrate.ft_candidates("FREETOKEN_MTP_MIN_P",
                                   dict.fromkeys(FT_ALL, "0.3")
                                   | {"FREETOKEN_MTP_MIN_P": "0.3"}) == ["0.5"]


def test_freetoken_sweep_adopts_writes_env_and_reports(tmp_path):
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken",
            "values": {}, "on": {}, "env": {"FREETOKEN_MTP_MIN_P": "0.25"}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    get_scores = _ft_scores(base=10.0, wins={"baseline": 10.0, "min_p=0.3": 12.0,
                                             "min_p=0.5": 11.5})
    seen = get_scores(mgr)

    job = _run_ft_job(mgr, prof)
    assert job["status"] == "done", job.get("error")
    assert job["steps"] == ["sweep"]
    rep = job["report"]
    assert rep["tuned"] and rep["applied"] and rep["backend"] == "freetoken"
    st = rep["settings"]
    # +20% の min_p=0.3 だけ採用 / +12.5% に満たない 0.5 は見送り、既定も明示値で全 7 キー
    assert st["FREETOKEN_MTP_MIN_P"] == "0.3"
    assert set(st) == {p for p, _ in calibrate.ft_grid("")}   # none モード: chain 2 キーは対象外
    assert st["FREETOKEN_MTP_COLD_START"] == "0" and st["FREETOKEN_MTP_MIN_ACCEPTANCE"] == "0.35"
    assert rep["improvement_pct"] == 20.0
    # 次セルには採用後の winner が乗っている (採用済み min_p=0.3 が base、打つ値だけ差し替え)
    trial_minp05 = next(o for lbl, o in seen if lbl == "min_p=0.5")
    assert trial_minp05["FREETOKEN_MTP_MIN_P"] == "0.5"
    assert trial_minp05["FREETOKEN_MTP_REJECT_STREAK"] == "3"
    # profile env に書き戻り (既定値も明示 = default drift 防止)。LATENCY 計測 env は書かない
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]["env"]
    assert saved["FREETOKEN_MTP_MIN_P"] == "0.3" and saved["FREETOKEN_MTP_MIN_ACCEPTANCE"] == "0.35"
    assert "FREETOKEN_LATENCY" not in saved
    # 詳細レポート (全セルの内訳) は data/calibrations/ に保存
    detail = json.loads(next((tmp_path / "data" / "calibrations").glob("calibration-cal_*.json"))
                        .read_text())
    assert len(detail["cells"]) == 1 + sum(
        len(calibrate.ft_candidates(p, {})) for p, _ in calibrate.FREETOKEN_MTP_GRID)
    assert detail["latency"].startswith("[latency]")
    assert any("採用" in ln for ln in job["logs"])


def test_freetoken_baseline_failure_is_fatal(tmp_path):
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken", "values": {}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    _ft_scores(fail=("baseline",))(mgr)

    job = _run_ft_job(mgr, prof)
    assert job["status"] == "failed"
    assert "基準" in job["error"] and "boom" in job["error"]
    # 失敗時に env は書き換わっていない
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert "FREETOKEN_MTP_MIN_P" not in saved["env"]


def test_freetoken_trial_failure_skips_candidate_and_continues(tmp_path):
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken", "values": {}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    seen = _ft_scores(base=10.0, fail=("reject_streak=2",))(mgr)

    job = _run_ft_job(mgr, prof)
    assert job["status"] == "done", job.get("error")
    labels = [lbl for lbl, _ in seen]
    assert "reject_streak=2" in labels and "reject_streak=5" in labels   # 失敗の次も打つ
    assert job["report"]["improvement_pct"] == 0.0
    assert any("失敗" in ln and "継続" in ln for ln in job["logs"])


def test_freetoken_cancel_stops_before_first_probe(tmp_path):
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken", "values": {}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    seen = _ft_scores()(mgr)

    async def go():
        job = await mgr.start(prof, "freetoken")
        mgr.cancel(job["id"])
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "cancelled"
    assert seen == []        # 中断後セルは打たない
    assert mgr.active_id is None


def test_ft_run_cell_injects_env_pair_latency_and_stops(tmp_path):
    """_ft_run_cell は env と _resolved_env の両方に注入する (_resolved_env が env より優先され
    process.py 侧で使われるため)。LATENCY 計測 env は細胞専用で、停止は必ず行う。"""
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken", "values": {}, "on": {},
            "env": {"X": "1"}, "_resolved_env": {"X": "1"}}
    (tmp_path / "module" / "freetoken").mkdir(parents=True)

    class _Recorder(_FakeManager):
        def __init__(self):
            super().__init__()
            self.started = []

        async def start(self, profile, force=False, **kw):
            self.started.append(profile)
            sess = {"id": "s1", "profile_id": "p_ft", "port": 1919, "status": "running",
                    "health_path": "/health", "log": "logs/nope.log"}
            self.sessions["s1"] = sess
            return sess

    fake = _Recorder()
    mgr = _mgr(tmp_path, tmp_path, fake)

    async def _nojob(job, sess):
        return None

    async def _probe(job, sess, copy=False):
        return {"ok": True, "score": 42.0, "tokens": 128, "secs": 3.0, "probes": []}

    mgr._ft_wait_ready = _nojob
    mgr._ft_probe = _probe
    mgr._ft_latency_excerpt = lambda sess: "[latency] test"

    out = asyncio.run(mgr._ft_run_cell({"id": "cal_x", "logs": []}, prof,
                                       {"X": "1"}, "baseline", {"FREETOKEN_MTP_MIN_P": "0.3"}))
    clone = fake.started[0]
    assert clone["env"]["FREETOKEN_MTP_MIN_P"] == "0.3" and clone["env"]["X"] == "1"
    assert clone["env"]["FREETOKEN_LATENCY"] == "1"
    assert clone["_resolved_env"]["FREETOKEN_MTP_MIN_P"] == "0.3"   # 解決済み env にも効かせる
    assert out["ok"] and out["score"] == 42.0 and out["latency"] == "[latency] test"
    assert fake.stopped == ["s1"]                                   # 成功でも失敗でも停止


def test_summary_freetoken_shape():
    rep = calibrate._summary("freetoken", {"freetoken": {
        "winner": {"FREETOKEN_MTP_MIN_P": "0.3"}, "improvement_pct": 7.5, "latency": "[latency] x"}})
    assert rep == {"tuned": True, "backend": "freetoken", "applied": True,
                   "settings": {"FREETOKEN_MTP_MIN_P": "0.3"}, "improvement_pct": 7.5,
                   "latency": "[latency] x"}


def test_ft_wait_ready_waits_for_body_status_not_http_200(tmp_path, monkeypatch):
    """freetoken の /health は loading 中も HTTP 200 (body status で判定)。
    200 だけで抜けると warmup が 503 にぶつかる (実環境で観測済み) ので、
    body status が ok になるまで待ち、loading 進捗はログへ残す。"""
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    docs = [{"status": "loading", "phase": "weights",
             "progress": {"done_bytes": 2 ** 30, "total_bytes": 8 * 2 ** 30}},
            {"status": "loading", "phase": "warmup", "progress": {}},
            {"status": "ok", "model": "m"}]
    n = {"i": 0}

    def fake(url, auth=None):
        i = min(n["i"], len(docs) - 1)
        n["i"] += 1
        return True, docs[i]
    monkeypatch.setattr(calibrate, "ft_health_status", fake)
    real_sleep = asyncio.sleep

    async def fast(t):
        await real_sleep(0.01)
    monkeypatch.setattr(asyncio, "sleep", fast)

    job = {"id": "cal_x", "logs": []}
    sess = {"id": "s1", "port": 1, "health_path": "/health", "argv": []}
    mgr.manager.sessions["s1"] = {"id": "s1", "status": "starting"}
    asyncio.run(mgr._ft_wait_ready(job, sess))
    assert n["i"] == 3 and any("モデルをロード中です" in ln for ln in job["logs"])


def test_ft_wait_ready_error_status_is_immediate_failure(tmp_path, monkeypatch):
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    monkeypatch.setattr(calibrate, "ft_health_status",
                        lambda url, auth=None: (True, {"status": "error", "message": "worker died"}))
    job = {"id": "cal_x", "logs": []}
    sess = {"id": "s1", "port": 1, "health_path": "/health", "argv": []}
    mgr.manager.sessions["s1"] = {"id": "s1", "status": "starting"}
    try:
        asyncio.run(mgr._ft_wait_ready(job, sess))
        raise AssertionError("error status で抜けた")
    except RuntimeError as e:
        assert "worker died" in str(e)


def test_ft_health_status_reads_json_body():
    import types

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b'{"status": "loading", "phase": "weights"}'
    calls = []
    monkey_orig = calibrate.urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return _R()
    calibrate.urllib.request.urlopen = fake_urlopen
    try:
        ok, doc = calibrate.ft_health_status("http://x/health")
        assert ok and doc["status"] == "loading"
        ok, doc = calibrate.ft_health_status("http://x/health")   # 2 回目も同じ偽
        assert ok and doc["phase"] == "weights"
    finally:
        calibrate.urllib.request.urlopen = monkey_orig


def test_ft_probe_retries_transient_503_warmup(tmp_path, monkeypatch):
    """準備完了直後の一時的な 503 (maintenance 等) は warmup 側でも諦めずに待つ。"""
    from llm_launcher import bench
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    seq = [{"ok": False, "error": 'HTTP 503 {"error":"server unavailable: model is still loading"}'},
           {"ok": False, "error": "HTTP 503 {}"},
           {"ok": True, "out_tokens": 8, "secs": 0.5},
           {"ok": True, "out_tokens": 100, "secs": 4.0},
           {"ok": True, "out_tokens": 100, "secs": 10.0}]
    i = {"n": 0}

    def fake_req(base, path, payload, stream, timeout, auth):
        r = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        return dict(r)
    monkeypatch.setattr(bench, "_one_request", fake_req)
    monkeypatch.setattr(bench, "_first_model", lambda base, auth=None: "m")
    real_sleep = asyncio.sleep

    async def fast(t):
        await real_sleep(0.01)
    monkeypatch.setattr(asyncio, "sleep", fast)

    out = asyncio.run(mgr._ft_probe({"id": "c", "logs": []}, {"port": 1, "argv": []}))
    assert out["ok"] and i["n"] == 5, out
    assert out["score"] == round(200 / 14.0, 2)


# ---------------------------------------------------------------- FreeToken lookup (fork CLI 接続)
#
# --speculative lookup のプロファイルは fork 同梱の python -m freetoken.calibrate_lookup
# を CLI 実行する (Strata と同じ形)。launcher 側の契約は「spec JSON を渡す / 出力 JSON を
# 読んで勝った 4 キーだけ env に書く / 負けは applied:False で正直に残す」までで、
# 採用判定そのもの (MIN_GAIN・並行上限) はツールの所有。


def _ft_lookup_profile(**kw):
    base = {"id": "p_ftl", "name": "ft lookup", "backend": "freetoken-fork",
            "values": {"speculative": "lookup", "port": "1933"},
            "on": {}, "env": {"X": "1", "FREETOKEN_LOOKUP_MAX_DRAFT": "9"}}
    base.update(kw)
    return base


def _lookup_doc(job_id, won=True):
    tag = f"cal-{job_id}".replace("_", "-")   # ツールの _sanitize と同じ変換
    name = f"freetoken-lookup-{tag}.json"
    env = {"FREETOKEN_LOOKUP_MAX_DRAFT": "4", "FREETOKEN_LOOKUP_MIN_MATCH": "3",
           "FREETOKEN_LOOKUP_MIN_CONTEXT": "256", "FREETOKEN_LOOKUP_MIN_ACCEPTANCE": "0.7"}
    doc = {"schema": 1, "lookup_enabled": bool(won),
           "chosen": {"speculative": "lookup" if won else "none", "env": env,
                      "gain": 0.12 if won else 0.05, "parallel": 4,
                      "tok_s_at_parallel": 60.0 if won else 40.0,
                      "gain_at_parallel": 0.113 if won else -0.05,
                      "acceptance": 0.62, "tok_s": 66.0}}
    return name, doc, env


def test_freetoken_lookup_start_builds_cli_spec(tmp_path):
    prof = _ft_lookup_profile()
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    got = {}

    async def no_run(job, steps, profile):
        got["job"], got["steps"] = job, steps
    mgr._run = no_run

    async def go():
        return await mgr.start(prof, "freetoken-fork")
    job = asyncio.run(go())
    assert job["mode"] == "cli"
    steps = got["steps"]
    assert [s["name"] for s in steps] == ["calibrate-lookup"]
    argv = steps[0]["argv"]
    assert argv[1:3] == ["-m", "freetoken.calibrate_lookup"]
    assert "--tag" in argv and argv[argv.index("--tag") + 1] == f"cal-{job['id']}"
    spec_path = next((tmp_path / "data" / "calibrations").glob(f"calibration-{job['id']}.spec.json"))
    spec = json.loads(spec_path.read_text())
    assert spec["port"] == 1933
    assert spec["argv"][:2] == ["-m", "freetoken"] and "--speculative" in spec["argv"]
    # 前回の勝者 env を基準腕に漏らさない (ツールの規則を先に守る) / 他 env は保持
    assert spec["env"] == {"X": "1"}
    # MTP プロファイルは従来どおり in-process sweep
    prof2 = _ft_lookup_profile(id="p_ftm", backend="freetoken",
                               values={"speculative": "mtp", "port": "1934"})
    got.clear()
    mgr.active_id = None      # no_run は完了処理をしないので 1 件目を手で開放する

    async def go2():
        j = await mgr.start(prof2, "freetoken")
        await asyncio.sleep(0)
        return j
    j2 = asyncio.run(go2())
    assert j2["mode"] == "sweep" and [s["name"] for s in got["steps"]] == ["sweep"]


def test_freetoken_lookup_job_applies_four_env_keys(tmp_path):
    prof = _ft_lookup_profile()
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())

    async def fake_step(job, step):
        name, doc, _env = _lookup_doc(job["id"])
        (tmp_path / "data" / "calibrations" / name).write_text(json.dumps(doc))
        return 0, ""
    mgr._run_step = fake_step

    async def go():
        job = await mgr.start(prof, "freetoken-fork")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    rep = job["report"]
    assert rep["backend"] == "freetoken-lookup" and rep["applied"] is True
    assert rep["improvement_pct"] == 11.3 and rep["settings"]["FREETOKEN_LOOKUP_MAX_DRAFT"] == "4"
    assert "parallel=4" in rep["latency"]
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]["env"]
    assert saved["X"] == "1" and saved["FREETOKEN_LOOKUP_MAX_DRAFT"] == "4"
    assert saved["FREETOKEN_LOOKUP_MIN_ACCEPTANCE"] == "0.7"


def test_freetoken_lookup_losing_result_stays_honest(tmp_path):
    prof = _ft_lookup_profile()
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())

    async def fake_step(job, step):
        name, doc, _env = _lookup_doc(job["id"], won=False)
        (tmp_path / "data" / "calibrations" / name).write_text(json.dumps(doc))
        return 0, ""
    mgr._run_step = fake_step

    async def go():
        job = await mgr.start(prof, "freetoken-fork")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done"
    rep = job["report"]
    assert rep["applied"] is False and rep["settings"] == {}
    assert rep["improvement_pct"] == -5.0          # 並行上限で負けた事実をそのまま
    assert any("書き戻しません" in ln for ln in job["logs"])
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]["env"]
    assert saved == {"X": "1", "FREETOKEN_LOOKUP_MAX_DRAFT": "9"}   # 無変更


def test_freetoken_lookup_missing_report_fails_job(tmp_path):
    prof = _ft_lookup_profile()
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())

    async def fake_step(job, step):
        return 0, ""        # exit 0 でもレポートが無ければ失敗 (Strata の verify と同じ思想)
    mgr._run_step = fake_step

    async def go():
        job = await mgr.start(prof, "freetoken-fork")
        while mgr.jobs[job["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[job["id"]]
    job = asyncio.run(go())
    assert job["status"] == "failed"
    assert "レポートが読めません" in job["error"]


# ---------------------------------------------------------------- FreeToken machine-fit (--moe-ram-budget)
#
# 単調パラメータなので sweep 対象外: 起動ログに disk 層発動行が出た時だけ
# MemTotal-40 GiB で moe_ram_budget を固定し、RAM 多寡で挙動がブレるのを止める。

FT_FIT_LINE = ("MoE disk tier: banks 38.27 GiB exceed the 37.99 GiB host RAM budget; "
               "mapping 1 head+tail MoE layers from the NVMe sidecar")


def _big_mem(monkeypatch):
    # meminfo は machine-fit (MemTotal) と memory_guard (MemAvailable/SwapFree) 両用
    monkeypatch.setattr(calibrate, "meminfo", lambda: {"MemTotal": 127 * 2**30,
                                                       "MemAvailable": 60 * 2**30,
                                                       "SwapTotal": 0, "SwapFree": 0})


def test_ft_machine_fit_sets_budget_only_on_fired_line(tmp_path, monkeypatch):
    prof = {"id": "p_fit", "name": "f", "backend": "freetoken", "values": {}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    _big_mem(monkeypatch)
    job = {"id": "cal_fit", "logs": []}
    mgr._ft_machine_fit(job, prof, FT_FIT_LINE)
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert saved["values"]["moe_ram_budget"] == "87"        # 127 - 40
    assert prof["values"]["moe_ram_budget"] == "87"          # restart_after に効く (in-memory)
    assert any("machine-fit: disk 層発動を検出" in ln for ln in job["logs"])


def test_ft_machine_fit_skips_when_tight_or_quiet(tmp_path, monkeypatch):
    prof = {"id": "p_fit", "name": "f", "backend": "freetoken", "values": {}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    job = {"id": "cal_fit", "logs": []}
    # 行なし → やらない
    mgr._ft_machine_fit(job, prof, "normal boot, all pinned")
    assert any("やらない" in ln for ln in job["logs"])
    # 余白なし (60GiB 箱) → auto のまま
    monkeypatch.setattr(calibrate, "meminfo", lambda: {"MemTotal": 60 * 2**30})
    mgr._ft_machine_fit(job, prof, FT_FIT_LINE)
    assert any("auto のまま" in ln for ln in job["logs"])
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert "moe_ram_budget" not in (saved.get("values") or {})


def test_sweep_baseline_log_fires_machine_fit(tmp_path, monkeypatch):
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken", "values": {}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "engine.log").write_text(FT_FIT_LINE, "utf-8")
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    _big_mem(monkeypatch)

    async def fake_cell(job, profile, base_env, label, overrides, copy=False, spec=None):
        r = {"ok": True, "score": 10.0, "tokens": 128, "secs": 12.8, "latency": "",
             "label": label}
        if label == "baseline":
            r["log"] = "logs/engine.log"
        return r
    mgr._ft_run_cell = fake_cell

    job = _run_ft_job(mgr, prof)
    assert job["status"] == "done", job.get("error")
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert saved["values"]["moe_ram_budget"] == "87"        # env 勝ち値と同時に values も適合


def test_lookup_job_logs_fire_machine_fit(tmp_path, monkeypatch):
    prof = _ft_lookup_profile()
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    _big_mem(monkeypatch)
    ldir = tmp_path / "data" / "calibrations" / "logs"
    ldir.mkdir(parents=True)

    async def fake_step(job, step):
        name, doc, _env = _lookup_doc(job["id"])
        (tmp_path / "data" / "calibrations" / name).write_text(json.dumps(doc))
        (ldir / "base.log").write_text(FT_FIT_LINE, "utf-8")
        return 0, ""
    mgr._run_step = fake_step

    async def go():
        j = await mgr.start(prof, "freetoken-fork")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert saved["values"]["moe_ram_budget"] == "87"
    assert saved["env"]["FREETOKEN_LOOKUP_MAX_DRAFT"] == "4"   # env 適用と両立


def test_resume_after_follows_accept_window_when_unset(tmp_path):
    """RESUME_AFTER 未設定時の実効値は ACCEPT_WINDOW 追従 (scheduler.py)。
    winner 正規化が実効値を明示しないと、書き戻しが黙って挙動を変えてしまう。"""
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken", "values": {}, "on": {},
            "env": {"FREETOKEN_MTP_ACCEPT_WINDOW": "128"}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    seen = _ft_scores(base=10.0)(mgr)

    job = _run_ft_job(mgr, prof)
    assert job["status"] == "done", job.get("error")
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]["env"]
    assert saved["FREETOKEN_MTP_RESUME_AFTER"] == "128"    # 実効値で明示 (64 で上書きしない)
    assert saved["FREETOKEN_MTP_ACCEPT_WINDOW"] == "128"
    # 現在値 128 の候補は 32 だけ (128 は打たない) / 各セルの overrides にも 128 が乗る
    labels = [lbl for lbl, _ in seen]
    assert "resume_after=32" in labels and "resume_after=128" not in labels
    assert "accept_window=24" in labels and "accept_window=128" not in labels
    trial = next(o for lbl, o in seen if lbl == "resume_after=32")
    assert trial["FREETOKEN_MTP_ACCEPT_WINDOW"] == "128"


def test_machine_fit_scales_guard_with_tp_size(tmp_path, monkeypatch):
    """明示 cap は rank 数で割られるので、余白検査も rank 数分見る (tp2 固定だと tp4 で過小)。"""
    prof = {"id": "p_fit", "name": "f", "backend": "freetoken",
            "values": {"tp_size": "4"}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    _big_mem(monkeypatch)                       # 127 GiB → budget 87
    job = {"id": "cal_fit", "logs": []}
    mgr._ft_machine_fit(job, prof, FT_FIT_LINE)  # banks 38.27/rank × 4 + 8 > 87 → 見送り
    assert any("tp4" in ln for ln in job["logs"])
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert "moe_ram_budget" not in (saved.get("values") or {})


# ---------------------------------------------------------------- 起動 JSON 主体モード (Strata 方式) + chain グリッド
#
# FreeToken 系は values.config の起動 JSON ({python, argv, cwd, env, port, model_name} =
# fork の calibrate_lookup spec と同一形式) が起動構成の正になり、フォームは最小
# パラメータ (port / api_key / env 上書き) だけ担う。キャリブレーションの勝者 env と
# machine-fit の --moe-ram-budget はこの JSON へ自動記載される。


def _write_launch_json(tmp_path, prof_id="p_ftj", argv=None, env=None):
    d = tmp_path / "data" / "launch"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"freetoken-{prof_id}.json"
    p.write_text(json.dumps({
        "python": "/fork/.venv/bin/python",
        "argv": argv if argv is not None else ["-m", "freetoken", "--port", "1919", "--tp-size", "2"],
        "cwd": "/fork", "env": env if env is not None else {"X": "1"},
        "port": 1919, "model_name": "m"}), "utf-8")
    return p


def test_render_argv_launch_json_mode(tmp_path):
    from llm_launcher import backends
    j = _write_launch_json(tmp_path, env={"A": "1", "B": "2"})
    b = backends.BACKENDS["freetoken-fork"]
    prof = {"backend": "freetoken-fork",
            "values": {"config": str(j), "port": "1933"},
            "env": {"B": "9", "C": "3"}}
    argv = backends.render_argv(b, prof, python="/py", binary="", root=tmp_path)
    # JSON の python が正 (standalone の python -m freetoken.serve_spec と同じ起動になる)
    assert argv == ["/fork/.venv/bin/python", "-m", "freetoken",
                    "--port", "1933", "--tp-size", "2"]
    # JSON env が正、profile.env はユーザー上書き
    assert prof["_resolved_env"] == {"A": "1", "B": "9", "C": "3"}
    # python が無い JSON だけ launcher 解決の python へ戻る
    nj = _write_launch_json(tmp_path, prof_id="p_nopy", env={})
    doc = json.loads(nj.read_text("utf-8"))
    del doc["python"]
    nj.write_text(json.dumps(doc), "utf-8")
    prof2 = {"backend": "freetoken-fork", "values": {"config": str(nj)}, "env": {}}
    assert backends.render_argv(b, prof2, python="/py", binary="",
                                root=tmp_path)[0] == "/py"
    # 起動の正なので、欠損は黙って無視せずエラー
    bad = {"backend": "freetoken-fork", "values": {"config": str(tmp_path / "nope.json")}}
    try:
        backends.render_argv(b, bad, python="/py", binary="", root=tmp_path)
        raise AssertionError("欠損ファイルで raise しなかった")
    except ValueError as e:
        assert "見つかりません" in str(e)
    assert backends.replace_arg(["--x", "1"], "--y", "2") == ["--x", "1", "--y", "2"]
    assert backends.replace_arg(["--x", "1"], "--x", "9") == ["--x", "9"]


def test_ft_grid_gates_chain_on_mtp():
    mtp = dict(calibrate.ft_grid("mtp"))
    assert "FREETOKEN_MTP_LOOKUP_CHAIN" in mtp and "FREETOKEN_MTP_CHAIN_K" in mtp
    for spec in ("", "none", "lookup"):
        assert "FREETOKEN_MTP_LOOKUP_CHAIN" not in dict(calibrate.ft_grid(spec))
    # chain 長は chain 有効時のみ候補が出る (無効時の測定は全セル同一で無駄)
    assert calibrate.ft_candidates("FREETOKEN_MTP_CHAIN_K", {}) == []
    assert calibrate.ft_candidates("FREETOKEN_MTP_CHAIN_K",
                                   {"FREETOKEN_MTP_LOOKUP_CHAIN": "1"}) == ["1", "3"]


def test_sweep_mtp_measures_chain_with_copy_probe(tmp_path):
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken",
            "values": {"speculative": "mtp"}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    seen = _ft_scores(base=10.0, wins={"lookup_chain=1": 30.0})(mgr)

    job = _run_ft_job(mgr, prof)
    assert job["status"] == "done", job.get("error")
    labels = [lbl for lbl, _ in seen]
    assert "lookup_chain=1" in labels and "chain_k=1" in labels and "chain_k=3" in labels
    # chain を測る run は全セル copy プローブ付き (履歴反復が無いと chain は発動しない)
    assert mgr.ft_copies and all(mgr.ft_copies)
    # 採用: chain=1 だけ勝ち → CHAIN_K は既定 2 のまま明示
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]["env"]
    assert saved["FREETOKEN_MTP_LOOKUP_CHAIN"] == "1" and saved["FREETOKEN_MTP_CHAIN_K"] == "2"
    # セル数 = 基準 + base 14 + chain 1 + (chain 採用後の) chain_k 2
    assert len(seen) == 1 + 14 + 1 + 2


def test_sweep_chain_k_skipped_while_chain_off(tmp_path):
    prof = {"id": "p_ft", "name": "ft", "backend": "freetoken",
            "values": {"speculative": "mtp"}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    seen = _ft_scores(base=10.0)(mgr)          # chain=1 が勝たない → chain_k は打たない

    job = _run_ft_job(mgr, prof)
    assert job["status"] == "done", job.get("error")
    labels = [lbl for lbl, _ in seen]
    assert "lookup_chain=1" in labels and not any(l.startswith("chain_k") for l in labels)
    assert len(seen) == 1 + 14 + 1


def test_ft_probe_copy_adds_third_request(tmp_path, monkeypatch):
    from llm_launcher import bench
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    calls = []

    def fake_req(base, path, payload, stream, timeout, auth):
        calls.append(payload["messages"][0]["content"])
        return {"ok": True, "out_tokens": 100, "secs": 4.0}
    monkeypatch.setattr(bench, "_one_request", fake_req)
    monkeypatch.setattr(bench, "_first_model", lambda base, auth=None: "m")
    out = asyncio.run(mgr._ft_probe({"id": "c", "logs": []}, {"port": 1, "argv": []}, copy=True))
    assert out["ok"] and len(calls) == 4                 # warmup + 64 + 512 + copy
    assert "Copy the passage" in calls[3] and "Copy the passage" not in calls[1]
    assert out["score"] == 25.0 and out["probes"][-1]["copy"] is True
    calls.clear()
    asyncio.run(mgr._ft_probe({"id": "c", "logs": []}, {"port": 1, "argv": []}))
    assert len(calls) == 3                                # 非 mtp run は従来どおり


def test_sweep_config_mode_writes_launch_json(tmp_path):
    j = _write_launch_json(tmp_path, env={"X": "1"})
    prof = {"id": "p_ftj", "name": "ft", "backend": "freetoken",
            "values": {"config": str(j)}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    _ft_scores(base=10.0, wins={"min_p=0.3": 12.0})(mgr)

    job = _run_ft_job(mgr, prof)
    assert job["status"] == "done", job.get("error")
    doc = json.loads(j.read_text())
    assert doc["env"]["FREETOKEN_MTP_MIN_P"] == "0.3" and doc["env"]["X"] == "1"
    assert doc["argv"][0] == "-m"                          # argv は無傷
    # config 主体モードでは profile.env に書かない (JSON が唯一の正)
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert "FREETOKEN_MTP_MIN_P" not in saved.get("env", {})
    assert any("起動 JSON に適用" in ln for ln in job["logs"])


def test_machine_fit_config_mode_updates_json_argv(tmp_path, monkeypatch):
    j = _write_launch_json(tmp_path)
    prof = {"id": "p_ftj", "name": "f", "backend": "freetoken",
            "values": {"config": str(j)}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    _big_mem(monkeypatch)
    job = {"id": "cal_fit", "logs": []}
    mgr._ft_machine_fit(job, prof, FT_FIT_LINE)
    doc = json.loads(j.read_text())
    argv = doc["argv"]
    assert argv[argv.index("--moe-ram-budget") + 1] == "87"
    assert any("起動 JSON" in ln for ln in job["logs"])
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert "moe_ram_budget" not in (saved.get("values") or {})   # JSON 側だけが正


def test_lookup_config_mode_passes_json_as_config(tmp_path):
    j = _write_launch_json(tmp_path, prof_id="p_ftl",
                           argv=["-m", "freetoken", "--speculative", "lookup", "--port", "1919"])
    prof = {"id": "p_ftl", "name": "ft lookup", "backend": "freetoken-fork",
            "values": {"config": str(j), "port": "1944"}, "on": {}, "env": {}}
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    got = {}

    async def no_run(job, steps, profile):
        got["job"], got["steps"] = job, steps
    mgr._run = no_run

    async def go():
        return await mgr.start(prof, "freetoken-fork")
    job = asyncio.run(go())
    assert got["job"]["mode"] == "cli"                     # JSON argv の --speculative で判定
    argv = got["steps"][0]["argv"]
    assert argv[argv.index("--config") + 1] == str(j)      # 起動 JSON そのものが --config
    assert argv[argv.index("--port") + 1] == "1944"        # ユーザー port 上書き
    assert not list((tmp_path / "data" / "calibrations").glob("calibration-*.spec.json"))


def test_launch_json_endpoints(tmp_path, monkeypatch):
    from llm_launcher import app as app_mod
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    (tmp_path / "data" / "profiles.json").write_text(json.dumps([
        {"id": "p_x", "name": "ft x", "backend": "freetoken-fork",
         "values": {"port": "1919", "model_path": "/m"}, "on": {}, "env": {"E": "1"}},
    ]), "utf-8")
    monkeypatch.setattr(app_mod.ProcessManager, "resolve_python", lambda self, bid: "/fake/py")
    app = app_mod.create_app(tmp_path)

    async def go():
        st, r = await _asgi(app, "POST", "/api/profiles/p_x/launch-json")
        assert st == 200 and r["ok"], r
        path = r["path"]
        assert path.endswith("freetoken-p_x.json")
        doc = json.loads(Path(path).read_text())
        assert doc["argv"][:2] == ["-m", "freetoken"] and doc["env"] == {"E": "1"}
        assert doc["python"] == "/fake/py" and doc["port"] == 1919
        assert "--model-path" in doc["argv"]               # フォーム値からレンダリング済み
        st, profiles = await _asgi(app, "GET", "/api/profiles")
        assert next(p for p in profiles if p["id"] == "p_x")["values"]["config"] == path
        st, r2 = await _asgi(app, "GET", "/api/profiles/p_x/launch-json")
        assert r2["exists"] and r2["launch"]["argv"][:2] == ["-m", "freetoken"]
        edited = dict(doc)
        edited["env"] = {**doc["env"], "NEW": "2"}
        st, r3 = await _asgi(app, "PUT", "/api/profiles/p_x/launch-json",
                             {"text": json.dumps(edited)})
        assert st == 200 and json.loads(Path(path).read_text())["env"]["NEW"] == "2"
        st, err = await _asgi(app, "PUT", "/api/profiles/p_x/launch-json", {"text": "{bad"})
        assert st == 400
        # config 切替後の preview は JSON 主体 + 最小上書きだけ
        st, pv = await _asgi(app, "POST", "/api/preview", {
            "name": "ft x", "backend": "freetoken-fork",
            "values": {"config": path, "port": "1950"}, "on": {}, "env": {}})
        assert pv["ok"], pv
        assert pv["argv"][pv["argv"].index("--port") + 1] == "1950"
        assert "--model-path" in pv["argv"]
    asyncio.run(go())


# ---------------------------------------------------------------- FreeToken auto (投機モード自動選定)
#
# 「mtp(+chain) / lookup / none のどれがこの箱で最速か」はプロファイル現在値では決められない
# → 1 ジョブでフェーズ A (mtp sweep) / B (lookup CLI) / C (共通プローブ決勝) を走り、
# 単流最優を採用、ただし並行上限で 2% 超負ける構成は並行最優へ差し替える。
# LOOKUP_MAX_BATCH (既定 2) はツールが sweep しないので、決勝の lookup-mb セルで A/B する。

def _auto_setup(tmp_path, prof, tour, lookup_rc=0, lookup_won=True, mtp_winner=None):
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True, exist_ok=True)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True, exist_ok=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    calls = {"sweep": [], "final": [], "step": []}

    async def fake_sweep(job, profile, *, apply=True, spec_override=None):
        calls["sweep"].append((apply, spec_override))
        return {"winner": mtp_winner if mtp_winner is not None else
                {"FREETOKEN_MTP_MIN_P": "0.3", "FREETOKEN_MTP_COLD_START": "0"},
                "improvement_pct": 12.3}
    mgr._run_freetoken = fake_sweep

    async def fake_step(job, step):
        calls["step"].append(step["name"])
        if lookup_rc == 0:
            name, doc, _e = _lookup_doc(job["id"], won=lookup_won)
            (tmp_path / "data" / "calibrations" / name).write_text(json.dumps(doc))
        return lookup_rc, ""
    mgr._run_step = fake_step

    async def fake_final(job, profile, base_env, label, overrides, spec, par_n):
        calls["final"].append((label, dict(overrides), spec, par_n))
        sc = tour.get(label)
        if sc is None:
            return {"ok": False, "error": "no script"}
        return {"ok": True, "single": sc[0], "par": sc[1], "par_error": "", "latency": ""}
    mgr._ft_final_cell = fake_final
    return mgr, calls


def _auto_prof(**kw):
    base = {"id": "p_fta", "name": "ft auto", "backend": "freetoken",
            "values": {"max_running_requests": "4", "speculative": "lookup"},
            "on": {}, "env": {}}
    base.update(kw)
    return base


TOUR_MB = {"final-none": (50.0, 88.0), "final-mtp": (60.0, 92.0),
           "final-lookup": (66.0, 86.0), "final-lookup-mb": (66.0, 95.0)}


def test_auto_demotes_single_winner_that_loses_at_parallel(tmp_path):
    prof = _auto_prof()
    mgr, calls = _auto_setup(tmp_path, prof, TOUR_MB)

    async def go():
        j = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    assert calls["sweep"] == [(False, "mtp")]        # フェーズ A は適用なし・mtp 強制
    assert [c[0] for c in calls["final"] if c[0].startswith("final-")] == [
        "final-none", "final-mtp", "final-lookup", "final-lookup-mb"]
    mb_call = next(c for c in calls["final"] if c[0] == "final-lookup-mb")
    assert mb_call[1]["FREETOKEN_LOOKUP_MAX_BATCH"] == "4" and mb_call[2] == "lookup"
    assert mb_call[3] == 4                            # par_n = --max-running-requests
    rep = job["report"]
    assert rep["backend"] == "freetoken-auto" and rep["applied"] is True
    # 単流最優 lookup (66) は並行 86 < 95*0.98 → lookup-mb に差し替え
    assert rep["settings"]["--speculative"] == "lookup"
    assert rep["settings"]["FREETOKEN_LOOKUP_MAX_BATCH"] == "4"
    assert rep["settings"]["FREETOKEN_LOOKUP_MIN_ACCEPTANCE"] == "0.7"
    assert "差し替え" in rep["rule"] and rep["improvement_pct"] == 32.0
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert saved["values"]["speculative"] == "lookup"
    assert saved["env"]["FREETOKEN_LOOKUP_MAX_BATCH"] == "4"


def test_auto_keeps_single_winner_within_parallel_tolerance(tmp_path):
    prof = _auto_prof()
    tour = dict(TOUR_MB, **{"final-lookup": (66.0, 91.5), "final-lookup-mb": (66.0, 93.0)})
    mgr, _calls = _auto_setup(tmp_path, prof, tour)

    async def go():
        j = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done"
    rep = job["report"]
    assert rep["rule"] == "単流スコア最優"            # 91.5 >= 93*0.98 → 差し替えなし
    assert "FREETOKEN_LOOKUP_MAX_BATCH" not in rep["settings"]
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert "FREETOKEN_LOOKUP_MAX_BATCH" not in saved["env"]


def test_auto_mtp_winner_writes_mtp_env_and_mode(tmp_path):
    prof = _auto_prof()
    tour = {"final-none": (50.0, 88.0), "final-mtp": (70.0, 95.0),
            "final-lookup": (66.0, 86.0), "final-lookup-mb": (66.0, 90.0)}
    mgr, _calls = _auto_setup(tmp_path, prof, tour)

    async def go():
        j = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    rep = job["report"]
    assert rep["settings"]["--speculative"] == "mtp"
    assert rep["settings"]["FREETOKEN_MTP_MIN_P"] == "0.3"
    assert rep["improvement_pct"] == 40.0
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert saved["values"]["speculative"] == "mtp"     # lookup → mtp へ書き換え
    assert saved["env"]["FREETOKEN_MTP_MIN_P"] == "0.3"


def test_auto_lookup_phase_failure_only_drops_lookup(tmp_path):
    prof = _auto_prof()
    tour = {"final-none": (50.0, 88.0), "final-mtp": (60.0, 92.0)}
    mgr, calls = _auto_setup(tmp_path, prof, tour, lookup_rc=1)

    async def go():
        j = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    assert [c[0] for c in calls["final"] if c[0].startswith("final-")] == ["final-none", "final-mtp"]
    assert any("lookup CLI が失敗" in ln for ln in job["logs"])
    assert job["report"]["settings"]["--speculative"] == "mtp"


def test_auto_config_mode_writes_launch_json(tmp_path):
    j = _write_launch_json(tmp_path, prof_id="p_fta",
                           argv=["-m", "freetoken", "--speculative", "none",
                                 "--port", "1919", "--max-running-requests", "4"])
    prof = _auto_prof(values={"config": str(j), "max_running_requests": "4"})
    mgr, _calls = _auto_setup(tmp_path, prof, TOUR_MB)

    async def go():
        jj = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[jj["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[jj["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    doc = json.loads(j.read_text())
    assert doc["argv"][doc["argv"].index("--speculative") + 1] == "lookup"   # none → lookup
    assert doc["env"]["FREETOKEN_LOOKUP_MAX_BATCH"] == "4"
    assert doc["env"]["X"] == "1"
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert saved["values"].get("speculative") is None   # legacy values 側は触らない
    assert "FREETOKEN_LOOKUP_MAX_BATCH" not in saved.get("env", {})


def test_auto_start_wiring_and_api_mode_validation(tmp_path):
    prof = _auto_prof()
    _seed_profiles(tmp_path, prof)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    mgr = _mgr(tmp_path, tmp_path, _FakeManager())
    got = {}

    async def no_run(job, steps, profile):
        got["job"], got["steps"] = job, steps
    mgr._run = no_run

    async def go():
        return await mgr.start(prof, "freetoken", cal_mode="auto")
    job = asyncio.run(go())
    assert job["mode"] == "auto" and [s["name"] for s in got["steps"]] == ["auto"]


def test_api_calibrate_mode_validation(tmp_path):
    from llm_launcher import app as app_mod
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "profiles.json").write_text(json.dumps([
        {"id": "p_m", "name": "maya", "backend": "maya", "values": {}},
        {"id": "p_ft", "name": "ft", "backend": "freetoken", "values": {}},
    ]), "utf-8")
    app = app_mod.create_app(tmp_path)

    async def go():
        # auto は FreeToken 系専用 (Maya では開始前に拒否)
        st, err = await _asgi(app, "POST", "/api/calibrations",
                              {"profile_id": "p_m", "mode": "auto"})
        assert st == 400 and "FreeToken" in err["detail"]
        st, err = await _asgi(app, "POST", "/api/calibrations",
                              {"profile_id": "p_ft", "mode": "weird"})
        assert st == 400 and "current / auto" in err["detail"]
    asyncio.run(go())


def test_auto_mtp_phase_failure_only_drops_mtp(tmp_path):
    """MTP ヘッドの無いモデル等ではフェーズ A の基準セルが起動失敗する。
    auto 全体を落とさず、mtp をトーナメントから外して続行すること。"""
    prof = _auto_prof()
    tour = {"final-none": (50.0, 88.0), "final-lookup": (66.0, 92.0),
            "final-lookup-mb": (66.0, 93.0)}
    mgr, calls = _auto_setup(tmp_path, prof, tour)

    async def boom(job, profile, *, apply=True, spec_override=None):
        raise calibrate.CalibrateError("基準 (現在の設定) のエンジンが起動 / 計測できません: no mtp head")
    mgr._run_freetoken = boom

    async def go():
        j = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    assert [c[0] for c in calls["final"] if c[0].startswith("final-")] == ["final-none", "final-lookup", "final-lookup-mb"]
    assert any("MTP sweep 失敗" in ln for ln in job["logs"])
    assert job["report"]["settings"]["--speculative"] == "lookup"


def test_auto_phase_d_micro_tuning_adopts_only_safe_wins(tmp_path):
    """フェーズ D: 勝者構成に対する品質中立ノブの座標降下。
    採用条件は 単流 +2% 超 かつ 並行を 2% 超悪化させないこと。"""
    prof = _auto_prof()
    tour = dict(TOUR_MB, **{
        "micro-shm_ar_max_kib=320": (69.0, 95.0),       # +4.5% / 並行維持 → 採用
        "micro-dual_stream_decode=1": (68.0, 95.0),     # -1.4% → 不採用
        "micro-fetch_overlap_min_rows=1": (72.0, 90.0), # 単流は勝つが並行 90 < 95*0.98 → 不採用
        "micro-dual_gather_bpb=1": (70.0, 95.0),        # +1.4% < 2% → 不採用
        "micro-dual_gather_bpb=4": (75.0, 80.0),        # +8.7% だが並行悪化 → 不採用
    })
    mgr, calls = _auto_setup(tmp_path, prof, tour)

    async def go():
        j = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    rep = job["report"]
    # 採用された SHM_AR_MAX_KIB だけ settings/env に入る (勝者 env と合流)
    assert rep["settings"]["FREETOKEN_SHM_AR_MAX_KIB"] == "320"
    assert rep["settings"]["FREETOKEN_LOOKUP_MAX_BATCH"] == "4"
    for k in ("FREETOKEN_DUAL_STREAM_DECODE", "FREETOKEN_FETCH_OVERLAP_MIN_ROWS",
              "FREETOKEN_DUAL_GATHER_BPB"):
        assert k not in rep["settings"], k
    # improvement は微調整後の単流 (69) 基準: 69/50 - 1 = +38%
    assert rep["improvement_pct"] == 38.0
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert saved["env"]["FREETOKEN_SHM_AR_MAX_KIB"] == "320"
    assert "FREETOKEN_DUAL_GATHER_BPB" not in saved["env"]
    # 詳細レポートに micro 内訳 (adopted フラグ込み) が残る
    detail = json.loads(next((tmp_path / "data" / "calibrations")
                             .glob("calibration-cal_*.json")).read_text())
    by_label = {m["label"]: m for m in detail["micro"] if m.get("ok")}
    assert by_label["shm_ar_max_kib=320"]["adopted"] is True
    assert by_label["fetch_overlap_min_rows=1"]["adopted"] is False
    assert by_label["dual_gather_bpb=4"]["adopted"] is False
    # 微調整セルは勝者モード (lookup) + 勝者 env 上で走る
    micro_call = next(c for c in calls["final"] if c[0] == "micro-shm_ar_max_kib=320")
    assert micro_call[2] == "lookup" and micro_call[1]["FREETOKEN_LOOKUP_MAX_BATCH"] == "4"
    assert any("[micro] shm_ar_max_kib=320" in ln and "採用" in ln for ln in job["logs"])


def test_api_freetoken_defaults_to_full_auto(tmp_path):
    """UI に測定モード選択は無い (全部測りの 1 本)。API 既定で FreeToken は auto に入り、
    Maya / Strata は従来経路のまま。"""
    from llm_launcher import app as app_mod
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "module" / "freetoken").mkdir(parents=True)
    (tmp_path / "data" / "profiles.json").write_text(json.dumps([
        {"id": "p_ft", "name": "ft", "backend": "freetoken", "values": {}, "on": {}, "env": {}},
    ]), "utf-8")
    app = app_mod.create_app(tmp_path)

    async def go():
        st, job = await _asgi(app, "POST", "/api/calibrations", {"profile_id": "p_ft"})
        assert st == 201, job
        assert job["mode"] == "auto" and job["steps"] == ["auto"]
        # 裏で本物の auto が走り始める (空 module なので全セル起動失敗 → failed で終わる)。
        # キャンセルを効かせて終了まで待つ (子プロセスは module 欠落で即死するので漏れない)
        await _asgi(app, "POST", f"/api/calibrations/{job['id']}/cancel")
        for _ in range(600):
            _st, j2 = await _asgi(app, "GET", f"/api/calibrations/{job['id']}")
            if j2["status"] not in ("queued", "running"):
                assert j2["status"] in ("cancelled", "failed")
                return
            await asyncio.sleep(0.1)
        raise AssertionError("ジョブが 60 秒以内に終了しなかった")
    asyncio.run(go())


def test_auto_micro_skips_wiring_for_lookup_winner(tmp_path):
    """MTP_WIRING は mtp 勝者のときだけ測る (lookup では論理的に絶対に効かない)。
    それ以外の『この機材では no-op』ノブは他 PC 運用のため全部測る。"""
    prof = _auto_prof()
    mgr, calls = _auto_setup(tmp_path, prof, TOUR_MB)   # 勝者 lookup-mb

    async def go():
        j = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    labels = [c[0] for c in calls["final"] if c[0].startswith("micro-")]
    assert "micro-mtp_wiring=norm_mix_fc" not in labels
    # 他 PC で効くノブは no-op 機材でも測る (hybrid / CPU 実行 / prefill 系)
    for want in ("micro-hybrid_min_bs=1", "micro-hybrid_fetch=lowest_id",
                 "micro-cpu_moe_spin=0", "micro-locked_fetch_threads=1",
                 "micro-prefill_debt_s=1.0", "micro-adaptive_chunk_target_s=0.05",
                 "micro-h2d_blocks_per_bank=4", "micro-tp_bank_stagger=0"):
        assert want in labels, want


def test_auto_micro_adopts_wiring_for_mtp_winner(tmp_path):
    prof = _auto_prof()
    tour = {"final-none": (50.0, 88.0), "final-mtp": (70.0, 95.0),
            "final-lookup": (66.0, 86.0), "final-lookup-mb": (66.0, 90.0),
            "micro-mtp_wiring=norm_mix_fc": (75.0, 95.0)}     # +7.1% / 並行維持 → 採用
    mgr, calls = _auto_setup(tmp_path, prof, tour)

    async def go():
        j = await mgr.start(prof, "freetoken", cal_mode="auto")
        while mgr.jobs[j["id"]]["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
        return mgr.jobs[j["id"]]
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    rep = job["report"]
    assert rep["settings"]["--speculative"] == "mtp"
    assert rep["settings"]["FREETOKEN_MTP_WIRING"] == "norm_mix_fc"
    assert rep["improvement_pct"] == 50.0                    # 75/50 - 1
    saved = json.loads((tmp_path / "profiles.json").read_text())[0]
    assert saved["env"]["FREETOKEN_MTP_WIRING"] == "norm_mix_fc"
    assert "micro-mtp_wiring=norm_mix_fc" in [c[0] for c in calls["final"]]
