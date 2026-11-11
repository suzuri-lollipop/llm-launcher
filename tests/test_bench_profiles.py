"""Benchmark profiles (data/bench_profiles.json) and the decode-length fields."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from llm_launcher import bench, rbac
from llm_launcher.app import BenchBody
from llm_launcher.store import JsonStore


def store(tmp_path):
    return JsonStore(tmp_path / "bench_profiles.json", [])


def test_create_list_update_delete(tmp_path):
    bp = bench.BenchProfiles(store(tmp_path))
    a = bp.create("short c4", {"profile_id": "p1", "output_len": 8})
    b = bp.create("long c4", {"profile_id": "p1", "output_len": 8192})
    assert [p["name"] for p in bp.list()] == ["long c4", "short c4"]   # newest first
    assert bp.get(a["id"])["params"]["output_len"] == 8
    upd = bp.update(a["id"], "short c4 edited", {"profile_id": "p2", "output_len": 16})
    assert upd["name"] == "short c4 edited" and upd["params"]["profile_id"] == "p2"
    assert bp.delete(b["id"]) is True
    assert bp.delete(b["id"]) is False
    assert [p["name"] for p in bp.list()] == ["short c4 edited"]


def test_names_are_unique_and_trimmed(tmp_path):
    bp = bench.BenchProfiles(store(tmp_path))
    bp.create("  same  ", {})
    with pytest.raises(bench.BenchProfileError):
        bp.create("SAME", {})
    with pytest.raises(bench.BenchProfileError):
        bp.create("   ", {})
    with pytest.raises(bench.BenchProfileError):
        bp.create("x" * 81, {})


def test_limit_is_enforced(tmp_path, monkeypatch):
    bp = bench.BenchProfiles(store(tmp_path))
    monkeypatch.setattr(bp, "MAX", 2)
    bp.create("a", {})
    bp.create("b", {})
    with pytest.raises(bench.BenchProfileError):
        bp.create("c", {})


def test_bench_profile_paths_need_the_bench_capability():
    assert rbac.required_cap("GET", "/api/bench-profiles") == "bench"
    assert rbac.required_cap("POST", "/api/bench-profiles") == "bench"
    assert rbac.required_cap("DELETE", "/api/bench-profiles/bp_x") == "bench"


def test_persisted_as_plain_json(tmp_path):
    path = tmp_path / "bench_profiles.json"
    bp = bench.BenchProfiles(JsonStore(path, []))
    bp.create("one", {"output_len": 8})
    data = json.loads(path.read_text("utf-8"))
    assert data[0]["name"] == "one" and data[0]["id"].startswith("bp_")


def test_decode_fields_are_validated_and_kept():
    b = BenchBody(profile_id="p1", output_len=8192, min_tokens=8192, ignore_eos=True)
    assert b.to_params()["min_tokens"] == 8192 and b.to_params()["ignore_eos"] is True
    with pytest.raises(ValidationError):
        BenchBody(profile_id="p1", output_len=8, min_tokens=9)
    with pytest.raises(ValidationError):
        BenchBody(profile_id="p1", output_len=1, min_tokens=-1)


def test_payload_sends_min_tokens_only_when_asked(tmp_path):
    mgr = bench.BenchManager(JsonStore(tmp_path / "bench.json", []),
                             JsonStore(tmp_path / "profiles.json", []), fake_manager())
    params = {"output_len": 8192, "stream": False, "temperature": 0.0,
              "min_tokens": 0, "ignore_eos": False, "endpoint": "chat"}
    body = mgr._payload(params, "m", "p")
    assert "min_tokens" not in body and "ignore_eos" not in body
    body = mgr._payload({**params, "min_tokens": 128, "ignore_eos": True}, "m", "p")
    assert body["min_tokens"] == 128 and body["ignore_eos"] is True


class fake_manager:
    sessions: dict = {}


def test_api_create_list_update_delete(tmp_path):
    import asyncio

    from test_auth_users import Client, make_app

    client = Client(make_app(tmp_path))
    params = {"profile_id": "p1", "model": "m", "endpoint": "completions", "stream": True,
              "concurrency": [1], "num_prompts": 4, "input_len": 16, "output_len": 8,
              "min_tokens": 0, "ignore_eos": False, "temperature": 0.0, "warmup": 0,
              "request_timeout": 30, "ready_timeout": 30, "auto_start": False, "stop_after": False}

    async def go():
        st, p = await client.post("/api/bench-profiles", {"name": "short", "params": params})
        assert st == 201, p
        pid = p["id"]
        st, allp = await client.get("/api/bench-profiles")
        assert st == 200 and [x["name"] for x in allp["profiles"]] == ["short"]
        st, p2 = await client.put(f"/api/bench-profiles/{pid}",
                                  {"name": "short edited", "params": {**params, "output_len": 16}})
        assert st == 200 and p2["name"] == "short edited" and p2["params"]["output_len"] == 16
        st, _ = await client.delete(f"/api/bench-profiles/{pid}")
        assert st == 200
        st, allp = await client.get("/api/bench-profiles")
        assert st == 200 and allp["profiles"] == []
        st, err = await client.post("/api/bench-profiles", {"name": "bad", "params": {**params, "mode": "nope"}})
        assert st == 400 and "ベンチ設定" in err["detail"]

    asyncio.run(go())


def test_bench_js_has_the_profile_and_decode_controls():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static"
          / "js" / "views" / "bench.js").read_text("utf-8")
    for needle in ("bm-bp", "bm-bp-name", "bmProfileSave", "bmProfileDelete",
                   "bm-decode", "bm-mintok", "bm-ignoreeos", "bmDecodeUI"):
        assert needle in js, needle
