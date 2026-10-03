"""Local results viewer. Reads saved benchmarks and profile interpretations."""

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from threading import RLock

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = {
    "test_create_batch_runs_500_10kb": "POST 500 × 10 KB",
    "test_create_batch_runs_50_100kb": "POST 50 × 100 KB",
    "test_get_run_10kb": "GET 10 KB",
    "test_get_run_100kb": "GET 100 KB",
}
CORE_STATS = {"min", "max", "mean", "median", "ops", "rounds", "iterations"}


class ReportStore:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.labels_path = self.directory / ".dashboard-labels.json"
        self.cache = {}
        self.lock = RLock()

    def labels(self):
        if not self.labels_path.exists():
            return {}
        try:
            labels = json.loads(self.labels_path.read_text())["labels"]
            if not isinstance(labels, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in labels.items()
            ):
                raise ValueError("invalid labels")
            return labels
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError(
                "Stage-name file is unreadable; it was not overwritten"
            ) from error

    def parse(self, path):
        data = json.loads(path.read_text())
        benchmarks = {}
        options = {}
        for entry in data["benchmarks"]:
            name = entry["name"]
            if name not in WORKLOADS:
                continue
            stats = entry["stats"]
            if name in benchmarks or not CORE_STATS.issubset(stats):
                raise ValueError("duplicate workload or missing timing statistics")
            for value in stats.values():
                if isinstance(value, (int, float)) and (
                    isinstance(value, bool) or not math.isfinite(value) or value < 0
                ):
                    raise ValueError("statistics must be finite, nonnegative numbers")
            if any(
                not isinstance(stats[key], (int, float)) or isinstance(stats[key], bool)
                for key in CORE_STATS
            ):
                raise ValueError("invalid core statistics")
            for key in ("rounds", "iterations"):
                if stats[key] < 1 or int(stats[key]) != stats[key]:
                    raise ValueError("rounds and iterations must be positive integers")
            samples = stats.get("data")
            if samples is not None and (
                not isinstance(samples, list)
                or len(samples) != stats["rounds"]
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    or value < 0
                    for value in samples
                )
            ):
                raise ValueError("invalid raw timing samples")
            benchmarks[name] = stats
            options[name] = entry.get("options", {})
        if set(benchmarks) != set(WORKLOADS):
            raise ValueError(
                "waiting for a report containing all four supplied workloads"
            )
        machine = data.get("machine_info", {})
        commit = data.get("commit_info", {})
        if (
            not isinstance(machine, dict)
            or not isinstance(machine.get("cpu", {}), dict)
            or not isinstance(commit, dict)
        ):
            raise ValueError("invalid machine or commit metadata")
        comparison = {
            "machine": machine,
            "options": options,
            "plugin_version": data.get("version"),
        }
        group = hashlib.sha256(
            json.dumps(comparison, sort_keys=True).encode()
        ).hexdigest()[:16]
        timestamp = data.get("datetime", "")
        try:
            order = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            order = path.stat().st_mtime
        return {
            "id": path.relative_to(self.directory).as_posix(),
            "group": group,
            "datetime": timestamp,
            "order": order,
            "machine": machine,
            "commit": commit,
            "options": options,
            "version": data.get("version"),
            "benchmarks": benchmarks,
        }

    def snapshot(self):
        with self.lock:
            reports, warnings, seen = [], [], set()
            for path in sorted(self.directory.rglob("*.json")):
                if path.name.startswith(".") or path.is_symlink():
                    continue
                if not path.resolve().is_relative_to(self.directory):
                    continue
                seen.add(path)
                cached = self.cache.get(path)
                try:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    if not cached or cached[0] != signature:
                        cached = (signature, self.parse(path))
                        self.cache[path] = cached
                    reports.append(dict(cached[1]))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    warnings.append(f"{path.name}: {error}")
                    # A file being rewritten must not erase its last complete result.
                    if cached:
                        reports.append(dict(cached[1]))
            self.cache = {
                path: value for path, value in self.cache.items() if path in seen
            }
            try:
                labels = self.labels()
            except ValueError as error:
                labels = {}
                warnings.append(str(error))
            reports.sort(key=lambda report: (report["order"], report["id"]))
            groups = {}
            for report in reports:
                group = report["group"]
                if group not in groups:
                    machine = report["machine"]
                    groups[group] = {
                        "id": group,
                        "label": (
                            f"{machine.get('system', 'Unknown OS')} · "
                            f"Python {machine.get('python_version', '?')} · "
                            f"{machine.get('cpu', {}).get('brand_raw', 'Unknown CPU')}"
                        ),
                        "count": 0,
                    }
                groups[group]["count"] += 1
                number = groups[group]["count"]
                report["label"] = labels.get(
                    report["id"], "Baseline" if number == 1 else f"Run {number}"
                )
            return {
                "reports": reports,
                "groups": list(groups.values()),
                "warnings": warnings,
                "workloads": WORKLOADS,
            }

    def rename(self, report_id, label):
        with self.lock:
            if report_id not in {report["id"] for report in self.snapshot()["reports"]}:
                raise KeyError(report_id)
            labels = self.labels()
            labels[report_id] = label
            # Only dashboard metadata is written. Original reports remain untouched.
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.directory, prefix=".dashboard-", delete=False
            ) as output:
                json.dump({"labels": labels}, output, indent=2)
                temporary = output.name
            try:
                os.replace(temporary, self.labels_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


class StageName(BaseModel):
    report_id: str = Field(min_length=1, max_length=1024)
    label: str = Field(min_length=1, max_length=80)


class InterpretationStore:
    """Read curated stage files; never run profilers or rewrite source reports."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    @staticmethod
    def source_path(source):
        path = Path(source["path"])
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] not in ("profiles", "measurements")
        ):
            raise ValueError("source must be in a local results directory")
        resolved = (ROOT / path).resolve()
        results_root = (ROOT / path.parts[0]).resolve()
        if not results_root.is_relative_to(
            ROOT.resolve()
        ) or not resolved.is_relative_to(results_root):
            raise ValueError("source escapes its results directory")
        return resolved

    def source_status(self, source):
        expected = source["sha256"]
        size = source["size_bytes"]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid source SHA-256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("invalid source byte size")
        path = self.source_path(source)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return "missing"
        if not path.is_file() or stat.st_size != size:
            return "changed"
        digest = hashlib.sha256()
        with path.open("rb") as recording:
            for chunk in iter(lambda: recording.read(1024 * 1024), b""):
                digest.update(chunk)
        return "verified" if digest.hexdigest() == expected else "changed"

    def snapshot(self):
        stages, warnings = [], []
        for path in sorted(self.directory.glob("*.json")):
            try:
                if path.is_symlink() or not re.fullmatch(r"[a-zA-Z0-9_-]+", path.stem):
                    raise ValueError("invalid interpretation filename")
                stage = json.loads(path.read_text())
                if stage["schema_version"] != 1 or stage["id"] != path.stem:
                    raise ValueError("unsupported schema or mismatched stage ID")
                if (
                    not isinstance(stage.get("order"), int)
                    or isinstance(stage["order"], bool)
                    or stage["order"] < 0
                ):
                    raise ValueError("stage order must be a nonnegative integer")
                for section in ("cpu", "memory"):
                    if not isinstance(stage[section]["workloads"], list):
                        raise ValueError("invalid workload results")
                if not isinstance(stage["findings"], list) or not isinstance(
                    stage["limitations"], list
                ):
                    raise ValueError("invalid interpretation sections")
                if not isinstance(stage["metric_sources"], list):
                    raise ValueError("missing metric source mapping")
                for metric in stage["metric_sources"]:
                    if not metric["source_ids"] or any(
                        source_id not in stage["sources"]
                        for source_id in metric["source_ids"]
                    ):
                        raise ValueError("metric references an unknown source")
                    if not isinstance(metric["rule"], str) or not metric["rule"]:
                        raise ValueError("missing metric derivation rule")
                mapped_sources = {
                    source_id
                    for metric in stage["metric_sources"]
                    for source_id in metric["source_ids"]
                }
                if mapped_sources != set(stage["sources"]):
                    raise ValueError("source list must match displayed metric inputs")
                counts = {"verified": 0, "changed": 0, "missing": 0}
                for source_id, source in stage["sources"].items():
                    if not re.fullmatch(r"[a-zA-Z0-9_-]+", source_id):
                        raise ValueError("invalid source ID")
                    status = self.source_status(source)
                    source["status"] = status
                    source["available"] = status == "verified"
                    counts[status] += 1
                    source[
                        "url"
                    ] = f"/api/interpretations/{stage['id']}/sources/{source_id}"
                stage["source_verification"] = {"total": sum(counts.values()), **counts}
                if counts["changed"] or counts["missing"]:
                    warnings.append(
                        f"{stage['id']}: {counts['changed']} changed, "
                        f"{counts['missing']} missing pinned source files"
                    )
                stage["download_url"] = f"/api/interpretations/{stage['id']}"
                stages.append(stage)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                warnings.append(f"{path.name}: {error}")
        orders = [stage["order"] for stage in stages]
        if len(orders) != len(set(orders)):
            warnings.append("profile interpretation stage orders must be unique")
        stages.sort(key=lambda stage: (stage["order"], stage["id"]))
        return {"stages": stages, "warnings": warnings}

    def stage(self, stage_id):
        for stage in self.snapshot()["stages"]:
            if stage["id"] == stage_id:
                return stage
        raise HTTPException(404, "Unknown profile interpretation")


def create_app(
    directory=ROOT / ".benchmarks",
    interpretations=ROOT / "reports" / "interpretations",
):
    store = ReportStore(directory)
    profile_store = InterpretationStore(interpretations)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/assets", StaticFiles(directory=ROOT / "dashboard"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "dashboard" / "index.html")

    @app.get("/api/reports")
    def reports():
        return store.snapshot()

    @app.get("/api/interpretations")
    def profile_interpretations():
        return profile_store.snapshot()

    @app.get("/api/interpretations/{stage_id}")
    def interpretation_file(stage_id: str):
        profile_store.stage(stage_id)
        return FileResponse(
            profile_store.directory / f"{stage_id}.json",
            filename=f"{stage_id}-profile-interpretation.json",
        )

    @app.get("/api/interpretations/{stage_id}/sources/{source_id}")
    def profile_source(stage_id: str, source_id: str):
        source = profile_store.stage(stage_id)["sources"].get(source_id)
        if not source:
            raise HTTPException(404, "Unknown profile source")
        path = profile_store.source_path(source)
        if source["status"] == "missing":
            raise HTTPException(404, "Source artifact is not present on this machine")
        if source["status"] != "verified":
            raise HTTPException(
                409, "Source artifact differs from the pinned recording"
            )
        if path.suffix == ".txt":
            return FileResponse(path, media_type="text/plain")
        return FileResponse(path, filename=path.name)

    @app.post("/api/stage-name")
    def stage_name(value: StageName, request: Request):
        if request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(
                403, "Stage names can only be edited from this dashboard"
            )
        label = value.label.strip()
        if not label:
            raise HTTPException(422, "Stage name cannot be blank")
        try:
            store.rename(value.report_id, label)
        except KeyError as error:
            raise HTTPException(404, "Unknown report") from error
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from error
        return {"saved": True}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reports", type=Path, default=ROOT / ".benchmarks")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    print(f"Benchmark dashboard: http://127.0.0.1:{args.port}", flush=True)
    print(
        f"Reading reports from {args.reports.resolve()}; no tests or services started"
    )
    uvicorn.run(create_app(args.reports), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
