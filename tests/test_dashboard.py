"""Dashboard-only tests; no database, S3, or benchmark execution."""

import hashlib
import json
import math

from fastapi.testclient import TestClient

from scripts.benchmark_dashboard import (
    ROOT,
    WORKLOADS,
    InterpretationStore,
    ReportStore,
    create_app,
)


def report():
    stats = {
        "min": 1.0,
        "max": 2.0,
        "mean": 1.5,
        "median": 1.5,
        "ops": 2 / 3,
        "rounds": 2,
        "iterations": 1,
        "data": [1.0, 2.0],
    }
    return {
        "datetime": "2026-10-02T12:00:00+00:00",
        "machine_info": {"system": "Test OS", "python_version": "3.11"},
        "version": "5.1.0",
        "benchmarks": [
            {"name": name, "stats": dict(stats), "options": {"warmup": False}}
            for name in WORKLOADS
        ],
    }


def test_reports_update_safely_and_keep_comparison_sets_separate(tmp_path):
    first = tmp_path / "0001.json"
    data = report()
    first.write_text(json.dumps(data))
    store = ReportStore(tmp_path)
    snapshot = store.snapshot()
    assert len(snapshot["reports"]) == 1
    assert snapshot["reports"][0]["label"] == "Baseline"
    assert len(snapshot["reports"][0]["benchmarks"]) == 4
    original = first.read_bytes()
    store.rename("0001.json", "Baseline <not markup>")
    assert first.read_bytes() == original
    assert ReportStore(tmp_path).snapshot()["reports"][0]["label"] == (
        "Baseline <not markup>"
    )

    second = tmp_path / "0002.json"
    second.write_text('{"benchmarks":')
    snapshot = store.snapshot()
    assert len(snapshot["reports"]) == 1 and snapshot["warnings"]
    data["datetime"] = "2026-10-02T13:00:00+00:00"
    second.write_text(json.dumps(data))
    snapshot = store.snapshot()
    assert len(snapshot["reports"]) == 2 and not snapshot["warnings"]
    assert snapshot["reports"][1]["label"] == "Run 2"
    assert len(snapshot["groups"]) == 1

    second.write_text("unfinished replacement")
    snapshot = store.snapshot()
    assert len(snapshot["reports"]) == 2 and snapshot["warnings"]
    data["benchmarks"][0]["options"]["warmup"] = True
    second.write_text(json.dumps(data))
    assert len(store.snapshot()["groups"]) == 2

    for entry in data["benchmarks"]:
        entry["stats"].pop("data")
    second.write_text(json.dumps(data))
    assert len(store.snapshot()["reports"]) == 2  # Samples are optional, not zero.
    invalid = report()
    invalid["benchmarks"][0]["stats"]["mean"] = float("nan")
    (tmp_path / "invalid.json").write_text(json.dumps(invalid))
    (tmp_path / "partial-suite.json").write_text(
        json.dumps({**report(), "benchmarks": report()["benchmarks"][:1]})
    )
    snapshot = store.snapshot()
    assert len(snapshot["reports"]) == 2 and len(snapshot["warnings"]) == 2
    second.unlink()
    assert len(store.snapshot()["reports"]) == 1


def test_profile_stages_use_explicit_order(tmp_path, monkeypatch):
    saved = tmp_path / "interpretations"
    saved.mkdir()
    baseline = json.loads(
        (ROOT / "reports" / "interpretations" / "baseline.json").read_text()
    )
    optimized = json.loads(
        (ROOT / "reports" / "interpretations" / "one-pass-encoder.json").read_text()
    )
    baseline["order"] = 1
    optimized["order"] = 0
    (saved / "baseline.json").write_text(json.dumps(baseline))
    (saved / "one-pass-encoder.json").write_text(json.dumps(optimized))
    store = InterpretationStore(saved)
    monkeypatch.setattr(store, "source_status", lambda source: "verified")

    snapshot = store.snapshot()

    assert [stage["id"] for stage in snapshot["stages"]] == [
        "one-pass-encoder",
        "baseline",
    ]
    assert not snapshot["warnings"]


def test_s3_session_reuse_interpretation_is_comparable(tmp_path, monkeypatch):
    saved = tmp_path / "interpretations"
    saved.mkdir()
    for name in ("baseline", "one-pass-encoder", "s3-session-reuse"):
        source = ROOT / "reports" / "interpretations" / f"{name}.json"
        (saved / source.name).write_bytes(source.read_bytes())
    store = InterpretationStore(saved)
    monkeypatch.setattr(store, "source_status", lambda source: "verified")

    snapshot = store.snapshot()

    assert not snapshot["warnings"]
    assert [stage["id"] for stage in snapshot["stages"]] == [
        "baseline",
        "one-pass-encoder",
        "s3-session-reuse",
    ]
    current = snapshot["stages"][-1]
    assert current["source_verification"]["verified"] == 13
    assert len(current["cpu"]["phase_medians"]) == 4
    assert len(current["memory"]["workloads"]) == 4
    assert current["cpu"]["workloads"][0]["functions"][3]["label"] == (
        "S3 client dependency"
    )
    for workload in current["memory"]["workloads"]:
        assert math.isclose(
            sum(row["mib"] for row in workload["allocations"]),
            workload["peak_mib"],
        )


def test_one_s3_read_interpretation(tmp_path, monkeypatch):
    saved = tmp_path / "interpretations"
    saved.mkdir()
    source = ROOT / "reports" / "interpretations" / "one-s3-read.json"
    (saved / source.name).write_bytes(source.read_bytes())
    store = InterpretationStore(saved)
    monkeypatch.setattr(store, "source_status", lambda source: "verified")

    snapshot = store.snapshot()

    assert not snapshot["warnings"]
    stage = snapshot["stages"][0]
    assert stage["order"] == 3
    assert stage["source_verification"]["verified"] == 11
    assert len(stage["cpu"]["phase_medians"]) == 4
    assert stage["cpu"]["workloads"][2]["functions"][1]["calls"] == 1
    for workload in stage["memory"]["workloads"]:
        assert math.isclose(
            sum(row["mib"] for row in workload["allocations"]),
            workload["peak_mib"],
        )


def test_dashboard_serves_assets_and_persists_only_valid_same_origin_labels(
    tmp_path, monkeypatch
):
    source = tmp_path / "baseline.json"
    source.write_text(json.dumps(report()))
    original = source.read_bytes()
    interpretation = json.loads(
        (ROOT / "reports" / "interpretations" / "baseline.json").read_text()
    )
    assert interpretation["cpu"]["condition"] == "Cold start"
    assert len(interpretation["cpu"]["workloads"]) == 4
    assert len(interpretation["cpu"]["phase_medians"]) == 4
    assert "diagnostics" not in interpretation
    assert len(interpretation["sources"]) == 14
    assert all(
        "/supplementary/" not in source["path"]
        for source in interpretation["sources"].values()
    )
    assert {item["metric"] for item in interpretation["metric_sources"]} == {
        "Cold CPU phase medians",
        "Cold CPU function rows",
        "Memory peaks and allocation groups",
    }
    assert interpretation["cpu"]["workloads"][0]["functions"][0]["calls"] == 1500
    for workload in interpretation["memory"]["workloads"]:
        assert math.isclose(
            sum(row["mib"] for row in workload["allocations"]), workload["peak_mib"]
        )
    saved = tmp_path / "interpretations"
    saved.mkdir()
    cpu_text = "Clock type: CPU\n"
    interpretation["sources"] = {
        "cpu-text": {
            "label": "CPU text",
            "path": "profiles/cpu.txt",
            "sha256": hashlib.sha256(cpu_text.encode()).hexdigest(),
            "size_bytes": len(cpu_text.encode()),
        },
        "missing": {
            "label": "Not present",
            "path": "profiles/missing.bin",
            "sha256": "0" * 64,
            "size_bytes": 0,
        },
    }
    interpretation["metric_sources"] = [
        {
            "metric": "Test CPU",
            "source_ids": ["cpu-text", "missing"],
            "rule": "Read the pinned text",
        }
    ]
    stage_file = saved / "baseline.json"
    stage_file.write_text(json.dumps(interpretation))
    stage_original = stage_file.read_bytes()
    with TestClient(create_app(tmp_path, saved), base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/dashboard.js").status_code == 200
        assert client.get("/assets/styles.css").status_code == 200
        assert client.get("/assets/profiles.js").status_code == 200
        page = client.get("/").text
        assert 'role="tablist"' in page
        assert 'aria-orientation="vertical"' in page
        assert 'id="sidebar-toggle"' in page
        assert 'id="dashboard-views"' in page
        assert 'id="profile-stage-nav"' in page
        assert 'aria-label="Profile stages"' in page
        assert 'id="profile-view-title"' in page
        assert 'id="profile-stage-summary"' in page
        assert 'id="profile-downloads"' in page
        assert 'id="profile-stage"' not in page
        assert 'id="comparison-set-control" hidden' in page
        assert "How results and stage names work" not in page
        assert client.get("/api/reports").json()["reports"][0]["label"] == "Baseline"
        payload = {"report_id": "baseline.json", "label": "Connection pooling"}
        assert client.post("/api/stage-name", json=payload).status_code == 403
        assert (
            client.post(
                "/api/stage-name",
                json=payload,
                headers={"Origin": "https://example.com"},
            ).status_code
            == 403
        )
        headers = {"Origin": "http://127.0.0.1"}
        assert (
            client.post("/api/stage-name", json=payload, headers=headers).status_code
            == 200
        )
        assert client.get("/api/reports").json()["reports"][0]["label"] == (
            "Connection pooling"
        )
        assert source.read_bytes() == original
        assert (
            client.post(
                "/api/stage-name", json={**payload, "label": " "}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/stage-name",
                json={**payload, "report_id": "../other.json"},
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.get("/api/reports", headers={"Host": "example.com"}).status_code
            == 400
        )
        labels = tmp_path / ".dashboard-labels.json"
        labels.write_text("corrupted stage names")
        assert (
            client.post("/api/stage-name", json=payload, headers=headers).status_code
            == 409
        )
        assert labels.read_text() == "corrupted stage names"

        # Source serving is allowlisted, including symlink / traversal protection.
        (tmp_path / "dashboard").mkdir()
        monkeypatch.setattr("scripts.benchmark_dashboard.ROOT", tmp_path)
        profile_folder = tmp_path / "profiles"
        profile_folder.mkdir()
        (profile_folder / "cpu.txt").write_text(cpu_text)
        result = client.get("/api/interpretations").json()
        assert len(result["stages"]) == 1 and "1 missing" in result["warnings"][0]
        assert result["stages"][0]["source_verification"] == {
            "total": 2,
            "verified": 1,
            "changed": 0,
            "missing": 1,
        }
        sources = result["stages"][0]["sources"]
        assert sources["cpu-text"]["available"] is True
        assert sources["missing"]["available"] is False
        assert client.get(sources["cpu-text"]["url"]).text == cpu_text
        assert client.get(sources["missing"]["url"]).status_code == 404
        (profile_folder / "cpu.txt").write_text("Clock type: GPU\n")
        changed = client.get("/api/interpretations").json()
        assert changed["stages"][0]["sources"]["cpu-text"]["status"] == "changed"
        assert "1 changed" in changed["warnings"][0]
        assert client.get(sources["cpu-text"]["url"]).status_code == 409
        (profile_folder / "cpu.txt").write_text(cpu_text)
        assert client.get(sources["cpu-text"]["url"]).status_code == 200
        assert (
            client.get("/api/interpretations/baseline/sources/not-listed").status_code
            == 404
        )
        assert client.get("/api/interpretations/not-listed").status_code == 404
        assert client.get("/api/interpretations/baseline").content == stage_original
        assert stage_file.read_bytes() == stage_original
        assert source.read_bytes() == original

        # A fresh app reads the saved interpretation without browser/server state.
        with TestClient(
            create_app(tmp_path, saved), base_url="http://127.0.0.1"
        ) as restarted:
            assert (
                restarted.get("/api/interpretations").json()["stages"][0]["title"]
                == "Baseline"
            )

        stage_file.write_text("unfinished JSON")
        result = client.get("/api/interpretations").json()
        assert not result["stages"] and result["warnings"]
        for unsafe in ("../outside.txt", "/etc/passwd", "profiles/../outside.txt"):
            interpretation["sources"]["cpu-text"]["path"] = unsafe
            stage_file.write_text(json.dumps(interpretation))
            result = client.get("/api/interpretations").json()
            assert not result["stages"] and result["warnings"]
        outside = tmp_path / "outside.txt"
        outside.write_text("not a profile")
        (profile_folder / "outside-link.txt").symlink_to(outside)
        interpretation["sources"]["cpu-text"]["path"] = "profiles/outside-link.txt"
        stage_file.write_text(json.dumps(interpretation))
        result = client.get("/api/interpretations").json()
        assert not result["stages"] and result["warnings"]
