#!/usr/bin/env python3
"""Automated S3 compatibility test suite for SeaweedFS / Tailscale endpoint."""

import argparse
import hashlib
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def get_secret(secret_name: str) -> str:
    res = subprocess.run(
        ["podman", "secret", "inspect", "--showsecret", "--format", "{{.SecretData}}", secret_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )
    return res.stdout.strip()


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_endpoint(endpoint: str, bucket: str, forbidden_bucket: str, access_key: str, secret_key: str):
    print(f"[*] Testing endpoint: {endpoint}")
    print(f"[*] Target bucket: {bucket}")
    print(f"[*] Forbidden bucket for cross-access test: {forbidden_bucket}")

    # 1. Healthz check
    health_url = f"{endpoint.rstrip('/')}/healthz"
    print(f"[1/8] Checking health check at {health_url}...")
    req = urllib.request.Request(health_url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Healthz returned unexpected status: {resp.status}")
    except Exception as e:
        raise RuntimeError(f"Healthz check failed: {e}")
    print("      -> Healthz OK (HTTP 200)")

    # 2. Boto3 client initialization with path addressing style
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )

    prefix = f"_infra_probe_{int(time.time())}"
    test_key_small = f"{prefix}/small.bin"
    test_key_presigned_put = f"{prefix}/presigned_put.bin"
    test_key_multipart = f"{prefix}/multipart.bin"

    try:
        # 3. Small object PUT & GET with SHA-256 verification
        print(f"[2/8] Testing PutObject & GetObject with probe key {test_key_small}...")
        payload_small = os.urandom(64 * 1024)  # 64 KiB
        expected_hash_small = compute_sha256(payload_small)
        s3.put_object(Bucket=bucket, Key=test_key_small, Body=payload_small)

        get_resp = s3.get_object(Bucket=bucket, Key=test_key_small)
        downloaded_small = get_resp["Body"].read()
        actual_hash_small = compute_sha256(downloaded_small)
        if actual_hash_small != expected_hash_small:
            raise RuntimeError(f"SHA-256 mismatch on small object! Expected {expected_hash_small}, got {actual_hash_small}")
        print(f"      -> Put/Get OK (SHA-256: {actual_hash_small[:16]}...)")

        # 4. Range GET verification (206 Partial Content)
        print("[3/8] Testing Range GET (bytes 100-199)...")
        range_resp = s3.get_object(Bucket=bucket, Key=test_key_small, Range="bytes=100-199")
        range_data = range_resp["Body"].read()
        if len(range_data) != 100 or range_data != payload_small[100:200]:
            raise RuntimeError(f"Range GET returned invalid data! Length: {len(range_data)}")
        content_range = range_resp.get("ContentRange", "")
        print(f"      -> Range GET OK (Content-Range: {content_range}, Length: {len(range_data)})")

        # 5. Presigned GET URL verification (unauthenticated client)
        print("[4/8] Testing Presigned GET URL (direct download without AWS credentials)...")
        presigned_get = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": test_key_small},
            ExpiresIn=300,
        )
        with urllib.request.urlopen(presigned_get, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Presigned GET failed with HTTP {resp.status}")
            presigned_download = resp.read()
        if compute_sha256(presigned_download) != expected_hash_small:
            raise RuntimeError("Presigned GET downloaded data does not match original SHA-256!")
        print(f"      -> Presigned GET OK (Downloaded {len(presigned_download)} bytes)")

        # 6. Presigned PUT URL verification (unauthenticated upload)
        print(f"[5/8] Testing Presigned PUT URL with probe key {test_key_presigned_put}...")
        payload_put = os.urandom(32 * 1024)  # 32 KiB
        expected_hash_put = compute_sha256(payload_put)
        presigned_put = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": test_key_presigned_put},
            ExpiresIn=300,
        )
        put_req = urllib.request.Request(presigned_put, data=payload_put, method="PUT")
        with urllib.request.urlopen(put_req, timeout=10) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Presigned PUT failed with HTTP {resp.status}")

        get_put_resp = s3.get_object(Bucket=bucket, Key=test_key_presigned_put)
        downloaded_put = get_put_resp["Body"].read()
        if compute_sha256(downloaded_put) != expected_hash_put:
            raise RuntimeError("Presigned PUT object verification failed!")
        print("      -> Presigned PUT OK")

        # 7. Multipart Upload via Presigned URLs (2 x 5 MiB parts)
        print(f"[6/8] Testing Multipart Upload via Presigned URLs ({test_key_multipart})...")
        part1 = os.urandom(5 * 1024 * 1024)
        part2 = os.urandom(5 * 1024 * 1024)
        full_payload = part1 + part2
        expected_full_hash = compute_sha256(full_payload)

        mpu = s3.create_multipart_upload(Bucket=bucket, Key=test_key_multipart)
        upload_id = mpu["UploadId"]
        parts_info = []

        for part_num, part_data in [(1, part1), (2, part2)]:
            part_url = s3.generate_presigned_url(
                "upload_part",
                Params={"Bucket": bucket, "Key": test_key_multipart, "UploadId": upload_id, "PartNumber": part_num},
                ExpiresIn=300,
            )
            req = urllib.request.Request(part_url, data=part_data, method="PUT")
            with urllib.request.urlopen(req, timeout=30) as resp:
                etag = resp.headers.get("ETag")
                if not etag:
                    raise RuntimeError(f"No ETag returned for part {part_num}")
                parts_info.append({"PartNumber": part_num, "ETag": etag.strip('"')})

        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=test_key_multipart,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts_info},
        )

        mp_resp = s3.get_object(Bucket=bucket, Key=test_key_multipart)
        downloaded_mp = mp_resp["Body"].read()
        if compute_sha256(downloaded_mp) != expected_full_hash:
            raise RuntimeError("Multipart downloaded object SHA-256 mismatch!")
        print(f"      -> Multipart Upload OK (Total: {len(downloaded_mp)} bytes, 2 parts)")

        # 8. Cross-bucket access isolation test
        print(f"[7/8] Testing Cross-bucket isolation against '{forbidden_bucket}'...")
        try:
            s3.list_objects_v2(Bucket=forbidden_bucket)
            raise RuntimeError(f"SECURITY VIOLATION: Credential accessed forbidden bucket '{forbidden_bucket}'!")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("AccessDenied", "403"):
                print(f"      -> Cross-bucket Denied as expected ({code})")
            else:
                print(f"      -> Cross-bucket rejected with code: {code}")

        # 9. Expiration test
        print("[8/8] Testing Presigned URL Expiration...")
        expired_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": test_key_small},
            ExpiresIn=1,
        )
        time.sleep(2)
        try:
            with urllib.request.urlopen(expired_url, timeout=5) as resp:
                raise RuntimeError("SECURITY VIOLATION: Expired URL was accepted!")
        except urllib.error.HTTPError as e:
            if e.code in (403, 400):
                print(f"      -> Expired URL rejected as expected (HTTP {e.code})")
            else:
                raise

        print("\n[SUCCESS] ALL S3 COMPATIBILITY TESTS PASSED!")

    finally:
        print("[*] Cleaning up probe objects...")
        for k in [test_key_small, test_key_presigned_put, test_key_multipart]:
            try:
                s3.delete_object(Bucket=bucket, Key=k)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Test S3 compatibility and presigned URLs")
    parser.add_argument("--endpoint", default="https://esillileu-server.tail4941d3.ts.net:9000", help="S3 Endpoint URL")
    parser.add_argument("--bucket", default="mlflow-f1-artifacts", help="Target bucket")
    parser.add_argument("--forbidden-bucket", default="mlflow-f2-artifacts", help="Forbidden bucket for access test")
    parser.add_argument("--access-key", help="AWS Access Key ID")
    parser.add_argument("--secret-key", help="AWS Secret Access Key")
    args = parser.parse_args()

    access_key = args.access_key
    secret_key = args.secret_key

    if not access_key:
        access_key = get_secret("mlflow_f1_s3_access_key")
    if not secret_key:
        secret_key = get_secret("mlflow_f1_s3_secret_key")

    test_endpoint(args.endpoint, args.bucket, args.forbidden_bucket, access_key, secret_key)


if __name__ == "__main__":
    main()
