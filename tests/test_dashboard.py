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
from scripts.memory_groups import GROUPING_VERSION, allocation_group


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

    history = tmp_path / ".history.json"
    history.write_text(
        json.dumps({"benchmarks": {"0001.json": "Rejected experiment"}})
    )
    archived_store = ReportStore(tmp_path, history)
    assert (
        archived_store.snapshot()["reports"][0]["history_reason"]
        == "Rejected experiment"
    )
    archived_store.rename("0001.json", "Looks retained after renaming")
    assert (
        archived_store.snapshot()["reports"][0]["history_reason"]
        == "Rejected experiment"
    )
    assert first.read_bytes() == original
    history.write_text("invalid history")
    invalid = archived_store.snapshot()
    assert any("history metadata" in warning for warning in invalid["warnings"])
    assert invalid["reports"][0]["history_reason"]


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
    history = tmp_path / "history.json"
    history.write_text(
        json.dumps({"profiles": {"one-pass-encoder": "History fixture"}})
    )
    store = InterpretationStore(saved, history)
    monkeypatch.setattr(store, "source_status", lambda source: "verified")

    snapshot = store.snapshot()

    assert [stage["id"] for stage in snapshot["stages"]] == [
        "one-pass-encoder",
        "baseline",
    ]
    assert not snapshot["warnings"]
    assert snapshot["stages"][0]["history_reason"] == "History fixture"
    assert snapshot["stages"][1]["history_reason"] is None


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


def test_get_optimization_interpretations(tmp_path, monkeypatch):
    saved = tmp_path / "interpretations"
    saved.mkdir()
    for name in (
        "one-s3-read",
        "whole-run-reference",
        "get-response-encoding",
        "bulk-postgres-writes",
        "spooled-post-buffer",
        "post-json-parsing",
    ):
        source = ROOT / "reports" / "interpretations" / f"{name}.json"
        (saved / source.name).write_bytes(source.read_bytes())
    store = InterpretationStore(saved)
    monkeypatch.setattr(store, "source_status", lambda source: "verified")

    snapshot = store.snapshot()

    assert not snapshot["warnings"]
    assert [stage["order"] for stage in snapshot["stages"]] == [3, 4, 5, 6, 7, 8]
    for stage, sources in zip(
        snapshot["stages"], (11, 15, 29, 35, 55, 56), strict=True
    ):
        assert stage["source_verification"]["verified"] == sources
        assert len(stage["cpu"]["phase_medians"]) == 4
        assert stage["cpu"]["workloads"][2]["functions"][1]["calls"] == 1
        for workload in stage["memory"]["workloads"]:
            assert stage["memory"]["grouping"]["version"] == GROUPING_VERSION
            assert workload["allocations"] == sorted(
                workload["allocations"], key=lambda row: (-row["mib"], row["label"])
            )
            assert all(
                "not classified" not in row["label"] for row in workload["allocations"]
            )
            assert math.isclose(
                sum(row["mib"] for row in workload["allocations"]),
                workload["peak_mib"],
            )
    current = snapshot["stages"][1]
    assert current["title"] == "One JSON decode per GET (not retained)"
    assert current["findings"][0]["status"] == "Not retained"
    assert current["commit"].startswith("87d8666")
    assert current["cpu"]["workloads"][0]["functions"][2]["calls"] == 500
    for workload in current["cpu"]["workloads"][2:]:
        decoder = workload["functions"][2]
        assert decoder["function"] == "orjson.loads" and decoder["calls"] == 1

    encoding = snapshot["stages"][2]
    assert encoding["title"] == "GET response encoding"
    assert encoding["review_status"].startswith("Uncommitted")
    assert encoding["cpu"]["workloads"][0]["functions"][2]["calls"] == 6000
    for workload in encoding["cpu"]["workloads"][2:]:
        assert workload["functions"][2]["calls"] == 3
        assert workload["functions"][3]["calls"] == 0
        assert workload["functions"][4]["calls"] == 1

    bulk = snapshot["stages"][3]
    assert bulk["title"] == "Bulk PostgreSQL writes"
    assert bulk["review_status"].startswith("Uncommitted")
    assert bulk["findings"][2]["status"] == "Mixed"
    post_memory = bulk["memory"]["workloads"][0]
    assert [row["label"] for row in post_memory["allocations"][:5]] == [
        "Benchmark-generated test data",
        "Unattributed (no Python stack)",
        "Decoded request JSON",
        "Batch buffer",
        "HTTP request body",
    ]
    assert math.isclose(post_memory["peak_mib"], 151.3925256729126)
    decoder = bulk["memory"]["workloads"][2]["allocations"][0]
    assert decoder == {"label": "Decode retrieved field JSON", "mib": 8}
    assert allocation_group([], []) == "Unattributed (no Python stack)"
    assert (
        allocation_group([("raw_decode", "/lib/json/decoder.py", 1)], [])
        == "Decoded request JSON"
    )
    assert (
        allocation_group([("body", "/lib/starlette/requests.py", 1)], [])
        == "HTTP request body"
    )
    assert (
        allocation_group([("validate", "/lib/fastapi/_compat.py", 1)], [])
        == "Model validation"
    )
    assert (
        allocation_group([("load", "/lib/botocore/loaders.py", 1)], [])
        == "S3 SDK / data"
    )
    assert (
        allocation_group(
            [("create_runs", "/app/api/routes/runs.py", 1)],
            ["batch_data = batch.getvalue()"],
        )
        == "Batch buffer"
    )
    assert (
        allocation_group(
            [("<listcomp>", "/app/api/routes/runs.py", 1)],
            ["orjson.loads(data[start:end])"],
        )
        == "Decode retrieved field JSON"
    )
    for workload, serializations in zip(
        bulk["cpu"]["workloads"][:2], (6000, 600), strict=True
    ):
        assert workload["functions"][2]["calls"] == serializations
        assert workload["functions"][4]["calls"] == 0
        assert workload["functions"][5]["calls"] == 1

    spooled = snapshot["stages"][4]
    assert spooled["title"] == "Spooled POST buffer (not retained)"
    assert spooled["review_status"].startswith("Not retained")
    assert spooled["findings"][0]["status"] == "Not retained"
    assert spooled["commit"].startswith("0def488")
    assert spooled["findings"][1]["status"] == "Trade-off"
    for workload, writes in zip(
        spooled["cpu"]["workloads"][:2], (502, 52), strict=True
    ):
        functions = {row["label"]: row for row in workload["functions"]}
        assert functions["Bulk COPY"]["calls"] == 1
        assert functions["Spool writes"]["calls"] == writes
        assert functions["Spill to disk"]["calls"] == 1
    for workload in spooled["memory"]["workloads"]:
        assert len(workload["peak_repeats_mib"]) == 3
        assert len(workload["source_ids"]) == 3
    assert len(spooled["comparison_results"]) == 4
    for row in spooled["comparison_results"][:2]:
        assert row["memory_reduction_pct"] > 0
        assert row["latency_reduction_pct"] < 0
        assert len(row["before_benchmark_medians_ms"]) == 3
        assert len(row["after_benchmark_medians_ms"]) == 3

    parsing = snapshot["stages"][-1]
    assert parsing["title"] == "POST JSON parsing (not retained)"
    assert parsing["review_status"].startswith("Not retained")
    assert parsing["commit"].startswith("49fbd67")
    assert parsing["findings"][0]["status"] == "Not retained"
    assert parsing["findings"][1]["title"] == "Cost of not retaining"
    assert parsing["memory"]["grouping"]["label_overrides"] == {
        "Model validation": "Request JSON parsing + validation"
    }
    for workload, serializations in zip(
        parsing["cpu"]["workloads"][:2], (6000, 600), strict=True
    ):
        functions = {row["label"]: row for row in workload["functions"]}
        assert functions["JSON serialization"]["calls"] == serializations
        assert functions["Bulk COPY"]["calls"] == 1
        assert functions["Separate request JSON decode"]["calls"] == 0
        assert functions["Direct request parser"]["calls"] == 1
        assert functions["JSON parsing + validation"]["calls"] == 1
    assert len(parsing["comparison_results"]) == 4
    for workload, row in zip(
        parsing["memory"]["workloads"], parsing["comparison_results"], strict=True
    ):
        assert len(workload["peak_repeats_mib"]) == 3
        assert len(workload["source_ids"]) == 3
        assert len(row["before_benchmark_medians_ms"]) == 3
        assert len(row["after_benchmark_medians_ms"]) == 3


def test_post_cleanup_history_distinguishes_measured_and_final_variants(
    tmp_path, monkeypatch
):
    saved = tmp_path / "interpretations"
    saved.mkdir()
    names = ("post-cleanup-control", "post-field-iteration", "post-cleanup-combined")
    for name in names:
        source = ROOT / "reports" / "interpretations" / f"{name}.json"
        (saved / source.name).write_bytes(source.read_bytes())
    store = InterpretationStore(saved)
    # Raw local recordings are ignored by Git; test the checked-in document
    # schema here, and verify actual source hashes during local publication.
    monkeypatch.setattr(store, "source_status", lambda source: "verified")
    snapshot = store.snapshot()
    assert not snapshot["warnings"]
    stages = snapshot["stages"]
    assert [stage["id"] for stage in stages] == list(names)
    assert [stage["order"] for stage in stages] == [9, 10, 11]
    assert [stage["measured_variant"] for stage in stages] == [
        "baseline",
        "fields",
        "both",
    ]
    assert len({stage["application_sha256"] for stage in stages}) == 3
    for index, stage in enumerate(stages):
        assert stage["comparison_stage_ids"] == list(names[: index + 1])
    for stage in stages:
        assert stage["classification"] == "experiment-history"
        assert stage["count_as_optimization"] is False
        assert stage["final_variant_measured"] is False
        assert stage["final_revision"].startswith("60dbc7d")
        assert stage["checkpoint_commit"].startswith("2e0ddc4")
        assert stage["commit"].startswith("b2fe475")
        assert "not measured" in stage["summary"]
        assert stage["findings"][0]["status"] == "History"
        assert len(stage["cpu"]["phase_medians"]) == 4
        assert len(stage["cpu"]["workloads"]) == 4
        assert len(stage["comparison_results"]) == 4
        benchmark_sources = [
            key for key in stage["sources"] if key.startswith("benchmark-")
        ]
        assert len(benchmark_sources) == 3
        for item, size in zip(stage["cpu"]["workloads"][:2], (500, 50), strict=True):
            functions = {row["label"]: row for row in item["functions"]}
            assert functions["Run dictionary conversion"]["calls"] == (
                size if stage["measured_variant"] == "baseline" else 0
            )
            assert functions["Response conversion"]["calls"] == (
                0 if stage["measured_variant"] == "both" else size + 5
            )
            assert functions["JSON serialization"]["calls"] == 12 * size + (
                stage["measured_variant"] == "both"
            )
            assert functions["Bulk COPY"]["calls"] == 1
        for item in stage["cpu"]["workloads"][2:]:
            functions = {row["label"]: row for row in item["functions"]}
            assert functions["S3 range read"]["calls"] == 1
            assert functions["JSON decode"]["calls"] == 3
        for item in stage["memory"]["workloads"]:
            assert len(item["peak_repeats_mib"]) == len(item["source_ids"]) == 3
            assert item["source_id"] in item["source_ids"]
            assert item["allocations"] == sorted(
                item["allocations"], key=lambda row: (-row["mib"], row["label"])
            )
            assert math.isclose(
                sum(row["mib"] for row in item["allocations"]), item["peak_mib"]
            )
        for item in stage["comparison_results"]:
            assert len(item["before_benchmark_medians_ms"]) == 3
            assert len(item["after_benchmark_medians_ms"]) == 3
    assert stages[-1]["comparison_results"][0]["before_variant"] == "fields"


def test_retained_post_parser_keeps_allocation_and_server_ram_separate(
    tmp_path, monkeypatch
):
    source = ROOT / "reports/interpretations/incremental-post-parsing.json"
    (tmp_path / source.name).write_bytes(source.read_bytes())
    store = InterpretationStore(tmp_path)
    monkeypatch.setattr(store, "source_status", lambda source: "verified")
    snapshot = store.snapshot()
    assert not snapshot["warnings"]
    stage = snapshot["stages"][0]
    assert stage["retention_decision"] == "retained"
    assert stage["parser_followup_complete"] is True
    assert stage["commit"].startswith("0d41d76")
    assert stage["source_verification"]["verified"] == 114
    assert stage["comparison_stage_ids"][-1] == stage["id"]
    assert "post-json-parsing" not in stage["comparison_stage_ids"]
    assert len(stage["cpu"]["phase_medians"]) == 4
    for item in stage["cpu"]["workloads"][:2]:
        assert item["functions"][0]["label"] == "Batch search"
        assert item["functions"][0]["calls"] == 0
    for item in stage["memory"]["workloads"]:
        assert len(item["peak_repeats_mib"]) == len(item["source_ids"]) == 3
        assert math.isclose(
            sum(row["mib"] for row in item["allocations"]), item["peak_mib"]
        )
    assert math.isclose(stage["memory"]["workloads"][0]["peak_mib"], 100.72517967224121)
    assert math.isclose(stage["memory"]["workloads"][1]["peak_mib"], 84.35297298431396)
    ram = stage["server_memory"]
    assert ram["metric"] == "OS lifetime peak worker RSS"
    assert len(ram["workloads"]) == 4
    for item in ram["workloads"]:
        assert item["concurrency"] in (1, 4)
        assert item["reduction_vs_original_pct"] > 25
        for variant in ("original", "before", "after"):
            assert len(item[variant]["repeats_mib"]) == 3
        assert item["after"]["peak_mib"] < item["before"]["peak_mib"]
    row = stage["comparison_results"][1]
    assert math.isclose(
        (row["after_median_ms"] / row["before_median_ms"] - 1) * 100, 10.443290560487672
    )
    assert sum(key.startswith("benchmark-") for key in stage["sources"]) == 6

    candidate = ROOT / "reports/interpretations/post-get-focus.json"
    (tmp_path / candidate.name).write_bytes(candidate.read_bytes())
    snapshot = store.snapshot()
    assert not snapshot["warnings"]
    stage = snapshot["stages"][-1]
    assert stage["retention_decision"] == "partially-retained"
    assert stage["count_as_optimization"] is False
    assert stage["component_decisions"]["get_decoder"] == (
        "not retained; reverted"
    )
    ram = stage["server_memory"]
    assert ram["mode"] == "get-decoder"
    assert len(ram["workloads"]) == 4
    assert ram["source_id"] in stage["sources"]
    for item in ram["workloads"]:
        for version in ("before", "after"):
            assert len(item[version]["first_peak_repeats_mib"]) == 3
            assert len(item[version]["warm_peak_repeats_mib"]) == 3
            assert len(item[version]["warm_latency_repeats_ms"]) == 3
        assert math.isclose(
            item["before"]["warm_peak_mib"] - item["after"]["warm_peak_mib"],
            item["warm_saving_mib"],
        )
        assert 1 < item["warm_reduction_pct"] < 3
    assert all(
        source_id.startswith("get-ram-")
        for source_id in (stage["metric_sources"][-1]["source_ids"])
    )

    frozen = ROOT / "reports/interpretations/frozen-final.json"
    (tmp_path / frozen.name).write_bytes(frozen.read_bytes())
    snapshot = store.snapshot()
    assert not snapshot["warnings"]
    stage = snapshot["stages"][-1]
    assert stage["optimization_frozen"] is True
    assert stage["count_as_optimization"] is False
    assert stage["component_decisions"] == {
        "get_decoder": "orjson restored; replacement not retained",
        "post_read_ahead": "kept",
        "s3_client_reuse": "deferred",
    }
    assert stage["comparison_stage_ids"] == ["baseline", "frozen-final"]
    assert stage["source_verification"]["verified"] == 71
    assert len(stage["cpu"]["phase_medians"]) == 4
    assert len(stage["comparison_results"]) == 4
    assert "server_memory" not in stage  # Historical RSS is not a new final capture.
    for item in stage["cpu"]["workloads"][2:]:
        functions = {row["label"]: row for row in item["functions"]}
        assert functions["S3 range read"]["calls"] == 1
        assert functions["JSON decode"]["calls"] == 3
    for item in stage["memory"]["workloads"]:
        assert len(item["peak_repeats_mib"]) == len(item["source_ids"]) == 3
        assert item["source_id"] in item["source_ids"]
        assert math.isclose(
            sum(row["mib"] for row in item["allocations"]), item["peak_mib"]
        )
    for row in stage["comparison_results"]:
        assert len(row["before_benchmark_medians_ms"]) == 1
        assert len(row["after_benchmark_medians_ms"]) == 3
    assert math.isclose(stage["memory"]["workloads"][2]["peak_mib"], 9.214622497558594)


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
    assert interpretation["memory"]["workloads"][2]["allocations"][0] == {
        "label": "Decode retrieved field JSON",
        "mib": 8,
    }
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
    history = tmp_path / ".history.json"
    history.write_text(
        json.dumps(
            {
                "benchmarks": {"baseline.json": "Archived fixture"},
                "profiles": {"baseline": "Archived fixture"},
            }
        )
    )
    with TestClient(
        create_app(tmp_path, saved, history), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/").status_code == 200
        benchmark_script = client.get("/assets/dashboard.js")
        assert benchmark_script.status_code == 200
        assert "Darwin-CPython-3.11-64bit/0006_baseline.json" in benchmark_script.text
        assert "displayedEntries(data.reports, 'benchmarks')" in benchmark_script.text
        assert client.get("/assets/styles.css").status_code == 200
        profile_script = client.get("/assets/profiles.js")
        assert profile_script.status_code == 200
        assert "displayedEntries(data.stages, 'profiles')" in profile_script.text
        assert "profiles: ['one-s3-read']" in benchmark_script.text
        assert "active.comparison_stage_ids.map" in profile_script.text
        assert "INTRODUCTION TO THE NEXT EXPERIMENT" not in profile_script.text
        assert "profile-next-experiment" not in profile_script.text
        assert (
            "['Decoded request JSON', 'Model validation', "
            "'Request JSON parsing + validation']" in profile_script.text
        )
        page = client.get("/").text
        assert 'role="tablist"' in page
        assert 'aria-orientation="vertical"' in page
        assert 'id="sidebar-toggle"' in page
        assert 'id="dashboard-views"' in page
        assert 'id="profile-stage-nav"' in page
        assert 'id="history-tab"' in page
        assert 'data-view="benchmarks">Benchmark</button>' in page
        assert 'data-view="profiles">Profiling</button>' in page
        assert 'id="history-panel"' in page
        assert 'aria-controls="history-panel"' in page
        assert 'aria-label="Archived profile interpretations"' in page
        assert 'aria-label="Archived benchmark reports"' in page
        assert 'id="profile-stage-history"' not in page
        assert 'id="benchmark-history"' not in page
        assert 'id="context"' not in page
        assert "RUN HANDLER / OPTIMIZATION EXPERIMENT" not in page
        assert (
            "Benchmark graphs and the profile evidence behind each optimization."
            not in page
        )
        assert 'aria-label="Profile stages"' in page
        assert 'id="profile-view-title"' in page
        assert 'id="profile-stage-summary"' in page
        assert 'id="profile-downloads"' in page
        assert 'id="profile-stage"' not in page
        assert 'id="comparison-set-control" hidden' in page
        assert "How results and stage names work" not in page
        assert client.get("/api/reports").json()["reports"][0]["label"] == "Baseline"
        assert client.get("/api/reports").json()["reports"][0]["history_reason"]
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
        assert result["stages"][0]["history_reason"] == "Archived fixture"
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
            create_app(tmp_path, saved, history), base_url="http://127.0.0.1"
        ) as restarted:
            assert (
                restarted.get("/api/interpretations").json()["stages"][0]["title"]
                == "Baseline"
            )
            assert restarted.get("/api/reports").json()["reports"][0]["history_reason"]

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
