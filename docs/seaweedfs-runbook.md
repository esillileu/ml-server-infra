# SeaweedFS / MLflow artifact 운영 runbook

이 구성은 단일 서버 rootless Podman 환경에서 운영되는 SeaweedFS 및 MLflow S3 artifact 스택이다.
모든 시각은 UTC 기준이다.

## 0. 역할과 원칙

- `[AGENT]`: 저장소 파일 작성과 정적·읽기 전용 검사만 수행한다.
- `[OPERATOR: svc-infra]`: image pull, secret 생성, Quadlet 설치, daemon reload 및 서비스 start/restart를 수행한다.
- `[OPERATOR: root]`: host filesystem mount, 디렉터리 권한, loginctl 등 root 권한 작업을 수행한다.
- 다른 bucket 접근 또는 anonymous 접근이 성공하면 운영을 중단한다.
- S3 ETag는 checksum으로 인정하지 않는다. 검증은 S3 내용을 다운로드해 SHA-256을 계산한다.

`svc-infra`는 의도적으로 비로그인 계정이다. 관리 셸은 다음과 같이 연다.

```bash
sudo -u svc-infra -H \
  env XDG_RUNTIME_DIR="/run/user/$(id -u svc-infra)" \
  /bin/bash
cd /opt/infra
```

## 1. 파일시스템 레이아웃 및 아카이브 상태

### Active State
- `/persist/srv/seaweedfs/master`: Master metadata
- `/persist/srv/seaweedfs/filer`: Filer metadata
- `/data/srv/seaweedfs/volume`: Bulk volume storage (ext4 HDD, owner `166535:166535`, mode `0750`)

### Inactive State (active 코드/서비스 참조 금지)
- `/persist/srv/seaweedfs/archive/f1-local-to-s3`: 과거 F1 S3 마이그레이션 보고서 보관소
- `/persist/srv/seaweedfs/rollback/volume-<UTC_TIMESTAMP>`: cutover 직전 old SSD volume rollback copy
- `/persist/srv/mlflow/archive/f1-pre-s3`: 과거 F1 로컬 파일시스템 원본 artifact 보관소

### Historical Note
- **F1 local filesystem → S3 migration**: Completed (모든 active artifact S3 보관 및 검증 완료)
- **SeaweedFS bulk volume SSD → `/data` HDD migration**: Completed (`seaweed-volume`으로 중립화 및 ext4 HDD 이관 완료)

## 2. 서비스 구조 및 의존성

User systemd Quadlet 서비스:
- `seaweed-master.service`: SeaweedFS Master (Port 9333)
- `seaweed-volume.service`: SeaweedFS Volume (Port 8080, `/data/srv/seaweedfs/volume:/data`)
  - 의존성: `Requires=seaweed-master.container`, `After=seaweed-master.container`
- `seaweed-filer.service`: SeaweedFS Filer (Port 8888)
  - 의존성: `Requires=seaweed-master.container seaweed-volume.container`, `After=seaweed-master.container seaweed-volume.container`
- `seaweed-s3.service`: SeaweedFS S3 Gateway (Port 9000:8333)
  - 의존성: `Requires=seaweed-filer.container`, `After=seaweed-filer.container`
- `mlflow_f1.service`: MLflow F1 (Port 5001:5000)
  - 의존성: `Requires=postgres.container seaweed-s3.container`, `After=postgres.container seaweed-s3.container`
- `mlflow_f2.service`: MLflow F2 (Port 5002:5000)
  - 의존성: `Requires=postgres.container seaweed-s3.container`, `After=postgres.container seaweed-s3.container`

### 서비스 기동 및 정지 순서
- **기동 순서**: Master → Volume → Filer → S3 → MLflow
- **정지 순서**: MLflow → S3 → Filer → Volume → Master

## 3. Credential 및 S3 IAM Secret 관리

`[OPERATOR: svc-infra]` credential은 Git에 저장하지 않고 Podman secret으로 관리한다.

```bash
umask 077
SEAWEED_SECRET_DIR="$(mktemp -d)"
for principal in admin f1 f2 corpus; do
  openssl rand -hex 10 | tr -d '\n' >"$SEAWEED_SECRET_DIR/$principal.access"
  openssl rand -base64 36 | tr -d '\n' >"$SEAWEED_SECRET_DIR/$principal.secret"
done
jq -n \
  --rawfile aa "$SEAWEED_SECRET_DIR/admin.access" --rawfile as "$SEAWEED_SECRET_DIR/admin.secret" \
  --rawfile f1a "$SEAWEED_SECRET_DIR/f1.access" --rawfile f1s "$SEAWEED_SECRET_DIR/f1.secret" \
  --rawfile f2a "$SEAWEED_SECRET_DIR/f2.access" --rawfile f2s "$SEAWEED_SECRET_DIR/f2.secret" \
  --rawfile ca "$SEAWEED_SECRET_DIR/corpus.access" --rawfile cs "$SEAWEED_SECRET_DIR/corpus.secret" \
  '{identities:[
    {name:"bootstrap-admin",credentials:[{accessKey:$aa,secretKey:$as}],actions:["Admin"]},
    {name:"mlflow-f1",credentials:[{accessKey:$f1a,secretKey:$f1s}],actions:["Read:mlflow-f1-artifacts","Write:mlflow-f1-artifacts","List:mlflow-f1-artifacts","Tagging:mlflow-f1-artifacts"]},
    {name:"mlflow-f2",credentials:[{accessKey:$f2a,secretKey:$f2s}],actions:["Read:mlflow-f2-artifacts","Write:mlflow-f2-artifacts","List:mlflow-f2-artifacts","Tagging:mlflow-f2-artifacts"]},
    {name:"f2-corpus",credentials:[{accessKey:$ca,secretKey:$cs}],actions:["Read:f2-corpus","Write:f2-corpus","List:f2-corpus"]}
  ]}' >"$SEAWEED_SECRET_DIR/s3.json"
podman secret create seaweed_s3_config "$SEAWEED_SECRET_DIR/s3.json"
podman secret create seaweed_s3_admin_access_key "$SEAWEED_SECRET_DIR/admin.access"
podman secret create seaweed_s3_admin_secret_key "$SEAWEED_SECRET_DIR/admin.secret"
podman secret create mlflow_f1_s3_access_key "$SEAWEED_SECRET_DIR/f1.access"
podman secret create mlflow_f1_s3_secret_key "$SEAWEED_SECRET_DIR/f1.secret"
podman secret create mlflow_f2_s3_access_key "$SEAWEED_SECRET_DIR/f2.access"
podman secret create mlflow_f2_s3_secret_key "$SEAWEED_SECRET_DIR/f2.secret"
podman secret create f2_corpus_s3_access_key "$SEAWEED_SECRET_DIR/corpus.access"
podman secret create f2_corpus_s3_secret_key "$SEAWEED_SECRET_DIR/corpus.secret"
rm -f "$SEAWEED_SECRET_DIR"/*
rmdir "$SEAWEED_SECRET_DIR"
unset SEAWEED_SECRET_DIR
```

버킷 초기화 및 credential 정합성 검증:
```bash
just seaweed bootstrap-buckets
```

f2-corpus credential rotation은 `scripts/seaweedfs/rotate-corpus-key.sh`를 사용한다.

```bash
just seaweed rotate-corpus-key
```

## 4. 운영 검증 및 Smoke 테스트

`[OPERATOR: svc-infra]`

### 4.1 상태 확인
```bash
just seaweed status
```
- F1/F2/S3 endpoint `UP` 및 seaweed 관련 컨테이너 상태 확인.

### 4.2 Known-good artifact 검증
`f1_known_good_artifact.json`에 정의된 과거 F1 artifact를 다운로드하여 SHA-256을 대조한다. 새 run/object를 생성하지 않는 순수 read 검증이다.

```bash
just seaweed verify-f1-known-good
```

### 4.3 Write smoke 테스트
MLflow 및 S3에 정상적인 쓰기/읽기가 이루어지는지 검증한다.

```bash
just seaweed smoke-f1
just seaweed smoke-f2
just seaweed smoke-multipart-f1
just seaweed smoke-multipart-f2
just seaweed test-s3-endpoint
```

## 5. Rollback 정책

HDD 볼륨 문제 발생 시 SSD 복귀 절차는 장애 발생 시점(신규 쓰기 발생 여부)에 따라 2가지로 구분된다.

### 5.1 Direct Rollback (신규 쓰기 발생 전)
신규 write가 발생하기 전(Read-only 검증 단계 실패 등)에는 데이터 변경이 없으므로 단순 mount 복귀를 수행한다.
- 서비스명은 `seaweed-volume`을 유지하며, `quadlet/seaweed-volume.container`의 볼륨 마운트만 SSD 경로로 변경:
  `Volume=/persist/srv/seaweedfs/volume-ssd:/data`
- `systemctl --user daemon-reload` 후 서비스 재기동

### 5.2 Post-write Reverse Sync Rollback (신규 쓰기 발생 후)
HDD에 이미 새 artifact/corpus 쓰기가 발생한 뒤 SSD로 복귀해야 하는 경우:
1. 모든 외부 writer freeze
2. 서비스 순차 정지 (MLflow → S3 → Filer → Volume → Master)
3. HDD $\to$ 선택한 rollback copy 디렉터리로 reverse sync (새 데이터 유실 방지):
   ```bash
   sudo rsync -a --delete --checksum \
     /data/srv/seaweedfs/volume/ \
     /persist/srv/seaweedfs/rollback/volume-<CUTOVER_ID>/
   ```
4. `quadlet/seaweed-volume.container`의 Volume mount를 해당 SSD rollback path로 변경
5. `systemctl --user daemon-reload`
6. 서비스 순차 재기동 (Master → Volume → Filer → S3 → MLflow)
7. read/write 검증 통과 후 writer freeze 해제

## 6. Tailnet S3 HTTPS 및 Presigned Multipart 운영

외부 분석 장비가 Tailnet을 통해 대용량 artifact를 병렬 chunk로 직접 업로드·다운로드할 수 있도록
Tailnet 전용 S3 HTTPS endpoint를 운영한다.

- **외부 S3 엔드포인트**: `https://esillileu-server.tail4941d3.ts.net:9000`
- **로컬 바인딩**: `127.0.0.1:9000` (Tailscale Serve가 `100.89.18.91:9000`에서 종단)
- **MLflow Quadlet 환경변수**:
  - `Environment=MLFLOW_S3_ENDPOINT_URL=https://esillileu-server.tail4941d3.ts.net:9000`
  - `Environment=MLFLOW_BOTO_CLIENT_ADDRESSING_STYLE=path`

### 6.1 Tailscale Serve 명령

```bash
# 활성화 (기존 443, 5432 설정 보존)
sudo tailscale serve --bg --https=9000 http://127.0.0.1:9000

# 비활성화 (롤백 시)
sudo tailscale serve --https=9000 off
```

### 6.2 외부 분석 장비 설정

외부 장비에서 대용량 artifact 직접 multipart 전송을 활성화하려면:

```bash
export MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD=true
export MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD=true
```

문제가 발생할 경우 환경변수를 `false`로 되돌리면 즉시 기존 MLflow proxy streaming 경로로 복귀한다.
