import hashlib
import os
import tempfile
from pathlib import Path

import mlflow

run_id = os.environ["RUN_ID"]
artifact_path = os.environ["ARTIFACT_PATH"]
expected = os.environ["EXPECTED_SHA256"]

with tempfile.TemporaryDirectory() as directory:
    downloaded = mlflow.artifacts.download_artifacts(
        run_id=run_id,
        artifact_path=artifact_path,
        dst_path=directory,
    )
    actual = hashlib.sha256(Path(downloaded).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"SHA-256 mismatch: expected={expected} actual={actual}")
    print(f"OK known-good run_id={run_id} artifact={artifact_path} sha256={actual}")
