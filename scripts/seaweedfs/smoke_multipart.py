import hashlib
import os
import tempfile
from pathlib import Path

import mlflow

# Force enable multipart upload and download
os.environ["MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD"] = "true"
os.environ["MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD"] = "true"

# Configure chunk thresholds to 10 MiB min size, 5 MiB chunk size
os.environ["MLFLOW_MULTIPART_UPLOAD_MINIMUM_FILE_SIZE"] = str(10 * 1024 * 1024)
os.environ["MLFLOW_MULTIPART_UPLOAD_CHUNK_SIZE"] = str(5 * 1024 * 1024)
os.environ["MLFLOW_MULTIPART_DOWNLOAD_MINIMUM_FILE_SIZE"] = str(10 * 1024 * 1024)
os.environ["MLFLOW_MULTIPART_DOWNLOAD_CHUNK_SIZE"] = str(5 * 1024 * 1024)

payload = os.urandom(15 * 1024 * 1024)  # 15 MiB -> triggers 3 chunks of 5 MiB
expected = hashlib.sha256(payload).hexdigest()

mlflow.set_experiment("multipart-smoke")
with tempfile.TemporaryDirectory() as directory:
    source = Path(directory, "multipart-sentinel.bin")
    source.write_bytes(payload)
    with mlflow.start_run(run_name="multipart-smoke-test") as run:
        print("Logging 15 MiB artifact to trigger presigned multipart upload...")
        mlflow.log_artifact(str(source), artifact_path="multipart")
        print("Downloading artifact to trigger presigned multipart download...")
        downloaded = mlflow.artifacts.download_artifacts(
            run_id=run.info.run_id,
            artifact_path="multipart/multipart-sentinel.bin",
            dst_path=str(Path(directory, "download")),
        )
        actual = hashlib.sha256(Path(downloaded).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"SHA-256 mismatch: expected={expected} actual={actual}")
        mlflow.log_param("multipart_sha256", expected)
        print(f"OK run_id={run.info.run_id} sha256={expected} (15 MiB multipart upload & download verified)")
