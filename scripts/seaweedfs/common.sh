#!/usr/bin/env bash
set -euo pipefail

readonly LOCAL_S3_ENDPOINT=http://127.0.0.1:9000

require_service_account() {
  if [[ "$(id -un)" != svc-infra || "$(id -u)" != 1001 ]]; then
    echo 'ERROR: run as svc-infra using an explicit shell:' >&2
    echo 'sudo -u svc-infra -H env XDG_RUNTIME_DIR="/run/user/$(id -u svc-infra)" /bin/bash' >&2
    exit 2
  fi
}

need() {
  command -v "$1" >/dev/null || { echo "ERROR: required command not found: $1" >&2; exit 2; }
}

secret_value() {
  podman secret inspect --showsecret --format '{{.SecretData}}' "$1"
}

make_rclone_config() {
  local access_secret=$1 key_secret=$2 config=$3 no_check_bucket=${4:-true}
  local access_key secret_key
  access_key="$(secret_value "$access_secret")"
  secret_key="$(secret_value "$key_secret")"
  umask 077
  {
    echo '[seaweed]'
    echo 'type = s3'
    echo 'provider = Other'
    echo 'env_auth = false'
    printf 'access_key_id = %s\n' "$access_key"
    printf 'secret_access_key = %s\n' "$secret_key"
    printf 'endpoint = %s\n' "$LOCAL_S3_ENDPOINT"
    echo 'region = us-east-1'
    echo 'force_path_style = true'
    printf 'no_check_bucket = %s\n' "$no_check_bucket"
  } >"$config"
  unset access_key secret_key
}
