"""「停止直後の起動で OS が固まる」 regresión ガードのテスト。

Strata 系エンジンは expert arena (数十 GiB) をピン留めするため、サーバー停止後も
エンジン子プロセスの解放(ピンの解除)に数十秒かかり、その間の起動は RAM 過コミットで
OS を固めうる。process.lingering_engine_pids() がそれを検出し、start() は force で
ない限り MemoryGuardError で拒否する。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from llm_launcher import process as procmod
from llm_launcher.oom import MemoryGuardError
from llm_launcher.process import ProcessManager, lingering_engine_pids
from llm_launcher.store import JsonStore


@pytest.fixture()
def fake_engine(tmp_path: Path):
    """cmdline が '<dir>/strata ... --serve' に見えるダミープロセス (実エンジンは使わない)。
    シェルスクリプトは argv[0] がインタプリタになり、coreutils は exec 名を検査するため、
    名前を検査しない python へのシンボリックリンクを strata という名前で作る。"""
    exe = tmp_path / "strata"
    exe.symlink_to(sys.executable)
    p = subprocess.Popen([str(exe), "-c", "import time; time.sleep(60)", "--serve"])
    try:
        yield p
    finally:
        p.kill()
        p.wait()


def test_lingering_engine_pids_sees_the_fake_engine(fake_engine):
    if sys.platform != "linux":
        pytest.skip("/proc は Linux のみ")
    assert fake_engine.pid in lingering_engine_pids()
    fake_engine.kill()
    fake_engine.wait()
    assert fake_engine.pid not in lingering_engine_pids()


def test_start_refuses_while_an_engine_is_draining(tmp_path: Path, fake_engine):
    if sys.platform != "linux":
        pytest.skip("/proc は Linux のみ")
    mgr = ProcessManager(tmp_path, JsonStore(tmp_path / "runs.json", []),
                         JsonStore(tmp_path / "profiles.json", []),
                         JsonStore(tmp_path / "settings.json", {}))
    profile = {"id": "p_test", "name": "t", "backend": "strata",
               "values": {"config": "module/strata/strata-x.json", "engine": "strata",
                          "host": "127.0.0.1", "port": "8099"},
               "on": {"config": True, "engine": True, "host": True, "port": True}}
    with pytest.raises(MemoryGuardError):
        asyncio.run(mgr.start(profile))
    # force はガードを通る (ここでは起動自体が失敗して ValueError/RuntimeError まで進むので
    # MemoryGuardError ではない = ガードを迂回できたことの確認)
    try:
        asyncio.run(mgr.start(profile, force=True))
    except MemoryGuardError:
        pytest.fail("force でも解放待ちガードで拒否された")
    except Exception:
        pass
