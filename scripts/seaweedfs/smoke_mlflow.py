import hashlib
import os
import tempfile
from pathlib import Path

import mlflow

payload = os.urandom(1024 * 1024)
expected = hashlib.sha256(payload).hexdigest()
mlflow.set_experiment("seaweedfs-smoke")
with tempfile.TemporaryDirectory() as directory:
    source = Path(directory, "seaweedfs-sentinel.bin")
    source.write_bytes(payload)
    with mlflow.start_run(run_name="artifact-smoke") as run:
        mlflow.log_artifact(str(source), artifact_path="sentinel")
        downloaded = mlflow.artifacts.download_artifacts(
            run_id=run.info.run_id, artifact_path="sentinel/seaweedfs-sentinel.bin",
            dst_path=str(Path(directory, "download")),
        )
        actual = hashlib.sha256(Path(downloaded).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"SHA-256 mismatch: expected={expected} actual={actual}")
        mlflow.log_param("sentinel_sha256", expected)
        print(f"OK run_id={run.info.run_id} sha256={expected}")
