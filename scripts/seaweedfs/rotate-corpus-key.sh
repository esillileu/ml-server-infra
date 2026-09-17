#!/usr/bin/env bash
set -euo pipefail

# 실행 계정 검사 (svc-infra / 1001)
if [[ "$(id -un)" != "svc-infra" || "$(id -u)" != "1001" ]]; then
  echo "ERROR: svc-infra 계정으로 실행해야 합니다." >&2
  echo "실행 방법:" >&2
  echo "  sudo -u svc-infra -H env XDG_RUNTIME_DIR=\"/run/user/1001\" $0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
  unset NEW_ACCESS_KEY NEW_SECRET_KEY OLD_CONFIG
}
trap cleanup EXIT
umask 077

echo "========================================================"
echo "      SeaweedFS F2 Corpus S3 자격 증명 교체 스크립트     "
echo "========================================================"
echo

# 1. 새 Access Key 입력 (화면 미표시, 2회 일치 확인)
echo "[1/2] Access Key 설정 (입력 내용이 화면에 표시되지 않으며 히스토리에 남지 않습니다)"
while true; do
  read -rsp "  새 Access Key 입력: " AK1
  echo
  if [[ -z "$AK1" ]]; then
    echo "  ! Access Key는 빈 값일 수 없습니다. 다시 입력해주세요." >&2
    continue
  fi

  read -rsp "  새 Access Key 확인 (한 번 더 입력): " AK2
  echo

  if [[ "$AK1" != "$AK2" ]]; then
    echo "  ! Access Key가 일치하지 않습니다. 다시 입력해주세요." >&2
    unset AK1 AK2
    continue
  fi

  NEW_ACCESS_KEY="$AK1"
  unset AK1 AK2
  echo "  -> Access Key 일치 확인 완료"
  break
done

echo
# 2. 새 Secret Key 입력 (화면 미표시, 2회 일치 확인)
echo "[2/2] Secret Key 설정 (입력 내용이 화면에 표시되지 않으며 히스토리에 남지 않습니다)"
while true; do
  read -rsp "  새 Secret Key 입력: " SK1
  echo
  if [[ -z "$SK1" ]]; then
    echo "  ! Secret Key는 빈 값일 수 없습니다. 다시 입력해주세요." >&2
    continue
  fi

  read -rsp "  새 Secret Key 확인 (한 번 더 입력): " SK2
  echo

  if [[ "$SK1" != "$SK2" ]]; then
    echo "  ! Secret Key가 일치하지 않습니다. 다시 입력해주세요." >&2
    unset SK1 SK2
    continue
  fi

  NEW_SECRET_KEY="$SK1"
  unset SK1 SK2
  echo "  -> Secret Key 일치 확인 완료"
  break
done

echo
echo "자격 증명이 확인되었습니다. 교체 작업을 진행합니다..."
echo

# 3. 임시 파일 생성
printf '%s' "$NEW_ACCESS_KEY" > "$TMP_DIR/corpus.access"
printf '%s' "$NEW_SECRET_KEY" > "$TMP_DIR/corpus.secret"

# 3. 기존 seaweed_s3_config에서 f2-corpus credentials만 교체
OLD_CONFIG="$(podman secret inspect --showsecret --format '{{.SecretData}}' seaweed_s3_config)"

jq --arg ak "$NEW_ACCESS_KEY" --arg sk "$NEW_SECRET_KEY" \
  '(.identities[] | select(.name == "f2-corpus").credentials[0]) |= {accessKey: $ak, secretKey: $sk}' \
  <<< "$OLD_CONFIG" > "$TMP_DIR/s3.json"
unset OLD_CONFIG

# 4. Podman Secret 교체 (--replace)
echo "[1/4] Podman Secret 갱신 중..."
podman secret create --replace seaweed_s3_config "$TMP_DIR/s3.json" >/dev/null
podman secret create --replace f2_corpus_s3_access_key "$TMP_DIR/corpus.access" >/dev/null
podman secret create --replace f2_corpus_s3_secret_key "$TMP_DIR/corpus.secret" >/dev/null

# 5. seaweed-s3 서비스 재시작
echo "[2/4] seaweed-s3 서비스 재시작 중..."
systemctl --user restart seaweed-s3.service

# 6. 헬스체크 대기
echo "[3/4] 서비스 정상 기동 대기 중..."
for i in {1..20}; do
  if curl -fsS http://127.0.0.1:9000/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# 7. 검증 스크립트 실행
echo "[4/4] 자격 증명 정합성 및 cross-bucket 격리 검증 중..."
"$SCRIPT_DIR/seaweed" bootstrap-buckets

echo
echo "========================================================"
echo "    자격 증명 교체 및 검증이 성공적으로 완료되었습니다!  "
echo "========================================================"
echo
echo "클라이언트 환경변수 파일(.env 등)에 다음 설정을 반영하세요:"
echo "--------------------------------------------------------"
echo "# 이 서버 로컬에서 실행할 때 (권장, 속도 빠름):"
echo "F2_CORPUS_S3_ENDPOINT=http://127.0.0.1:9000"
echo "# 외부 머신(노트북 등 Tailnet)에서 실행할 때:"
echo "# F2_CORPUS_S3_ENDPOINT=https://esillileu-server.tail4941d3.ts.net:9000"
echo "F2_CORPUS_S3_ROOT=s3://f2-corpus"
echo "F2_CORPUS_S3_ACCESS_KEY=<방금 입력하신 Access Key>"
echo "F2_CORPUS_S3_SECRET_KEY=<방금 입력하신 Secret Key>"
echo "--------------------------------------------------------"
