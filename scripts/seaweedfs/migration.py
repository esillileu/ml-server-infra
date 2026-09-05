#!/usr/bin/env python3
"""Create immutable F1 migration allowlists/manifests and verify reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import urllib.request
from pathlib import Path


def post_pages(base: str, endpoint: str, collection: str, body: dict) -> list[dict]:
    items: list[dict] = []
    token: str | None = None
    while True:
        request_body = dict(body)
        if token:
            request_body["page_token"] = token
        req = urllib.request.Request(
            base.rstrip("/") + endpoint,
            data=json.dumps(request_body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            page = json.load(response)
        items.extend(page.get(collection, []))
        token = page.get("next_page_token")
        if not token:
            return items


def mlflow_search(base: str) -> list[dict]:
    experiments = post_pages(
        base, "/api/2.0/mlflow/experiments/search", "experiments",
        {"max_results": 1000, "view_type": "ALL"},
    )
    experiment_ids = [str(item["experiment_id"]) for item in experiments]
    runs: list[dict] = []
    for start in range(0, len(experiment_ids), 100):
        runs.extend(post_pages(
            base, "/api/2.0/mlflow/runs/search", "runs",
            {"experiment_ids": experiment_ids[start:start + 100], "max_results": 1000, "run_view_type": "ALL"},
        ))
    return runs


def run_info(run: dict) -> tuple[str, str, str]:
    info = run["info"]
    return str(info["experiment_id"]), str(info["run_id"]), info["status"]


def is_active(run: dict) -> bool:
    return run["info"].get("lifecycle_stage", "active") == "active"


def regular_files(root: Path, prefixes: set[str] | None) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in list(dirnames) + filenames:
            path = current / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise RuntimeError(f"non-regular source entry: {path}")
        for name in filenames:
            path = current / name
            rel = path.relative_to(root)
            parts = rel.parts
            if prefixes is None or (len(parts) >= 3 and "/".join(parts[:2]) in prefixes and parts[2] == "artifacts"):
                found.append(rel)
    return sorted(found, key=lambda p: p.as_posix().encode())


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb", buffering=1024 * 1024) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def prepare(args: argparse.Namespace) -> None:
    root, report = Path(args.source), Path(args.report)
    runs = mlflow_search(args.mlflow_url)
    active_runs = [run for run in runs if is_active(run)]
    deleted = sorted(run_info(run)[1] for run in runs if not is_active(run))
    running = sorted(run_info(run)[1] for run in active_runs if run_info(run)[2] == "RUNNING")
    if args.mode == "final" and running:
        raise RuntimeError(f"final migration refused: {len(running)} RUNNING run(s)")
    selected = [run_info(run) for run in active_runs if args.mode == "final" or run_info(run)[2] != "RUNNING"]
    prefixes = None if args.mode == "final" else {f"{exp}/{run}" for exp, run, _ in selected}
    files = regular_files(root, prefixes)
    total = 0
    with (report / "sha256.txt").open("w") as hashes, (report / "paths.txt").open("w") as paths:
        for rel in files:
            size = (root / rel).stat().st_size
            total += size
            rel_text = rel.as_posix()
            hashes.write(f"{digest(root / rel)}  {rel_text}\n")
            paths.write(rel_text + "\n")
    (report / "runs.json").write_text(
        json.dumps({"selected": selected, "excluded_running": running, "deleted": deleted}, indent=2) + "\n"
    )
    (report / "summary.json").write_text(json.dumps({"mode": args.mode, "files": len(files), "bytes": total}, indent=2) + "\n")
    print(json.dumps({"selected_runs": len(selected), "excluded_running": running, "files": len(files), "bytes": total}))


def inspect(args: argparse.Namespace) -> None:
    root = Path(args.source)
    runs = mlflow_search(args.mlflow_url)
    running = sorted(run_info(run)[1] for run in runs if is_active(run) and run_info(run)[2] == "RUNNING")
    # Traversal itself rejects symlinks, devices, sockets, and other special entries.
    files = regular_files(root, set())
    print(json.dumps({"running": running, "special_entries": 0, "selected_files": len(files)}))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--source", required=True)
    prep.add_argument("--report", required=True)
    prep.add_argument("--mlflow-url", required=True)
    prep.add_argument("--mode", choices=("completed", "final"), required=True)
    check = sub.add_parser("inspect")
    check.add_argument("--source", required=True)
    check.add_argument("--mlflow-url", required=True)
    args = parser.parse_args()
    try:
        prepare(args) if args.command == "prepare" else inspect(args)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
