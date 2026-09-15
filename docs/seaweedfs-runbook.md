# SeaweedFS / MLflow artifact 운영 runbook

이 구성은 단일 서버 배치이며 백업이나 HA가 아니다. F1 원본
`/persist/srv/mlflow/mlflow_f1`은 이 절차에서 수정하거나 삭제하지 않는다. HDD도 다루지
않는다. 모든 시각과 보고서 디렉터리 이름은 UTC이다.

## 0. 역할과 중단 조건

- `[AGENT]`: 저장소 파일 작성과 정적·읽기 전용 검사만 수행한다.
- `[OPERATOR: svc-infra]`: image pull, secret 생성, Quadlet 설치, daemon reload 및 서비스
  start/restart를 수행한다.
- 다른 bucket 접근 또는 anonymous 접근이 성공하면 배포를 중단한다.
- 가용 공간이 `source bytes × 1.5 + 50,000,000,000 bytes` 미만이면 중단한다.
- F1 latency/error rate 악화 또는 disk utilization의 지속 포화 시 복사를 `Ctrl-C`로
  중단한다. `rclone copy --immutable`이므로 같은 명령으로 안전하게 재개할 수 있다.
- S3 ETag는 checksum으로 인정하지 않는다. 검증은 S3 내용을 다운로드해 SHA-256을
  계산한다.

`svc-infra`는 의도적으로 비로그인 계정이다. 따라서 이 문서에서는 `sudo -iu
svc-infra`나 계정의 login shell을 사용하지 않는다. 관리 계정에서 필요한 root 작업을
마친 다음, 실행 파일을 명시해 서비스 계정의 Bash를 연다. `XDG_RUNTIME_DIR`는 rootless
Podman과 user systemd가 `svc-infra`의 runtime 및 user bus를 사용하도록 고정한다.

```bash
sudo loginctl enable-linger svc-infra
sudo -u svc-infra -H \
  env XDG_RUNTIME_DIR="/run/user/$(id -u svc-infra)" \
  /bin/bash
cd /opt/infra
```

이후 `[OPERATOR: svc-infra]` 코드 블록은 위 Bash 안에서 실행한다. 셸을 나갔다면 같은
명령으로 다시 연다. `enable-linger`는 `svc-infra`의 user manager를 로그인과 독립적으로
유지한다. 이 명령 자체는 이 runbook의 Quadlet 서비스를 enable하지 않는다. 단, 이미
enable된 user unit이 있다면 user manager 시작 시 자동 기동될 수 있다.

## 1. 디렉터리와 image digest

관리 계정에서 먼저 host package와 영속 디렉터리를 준비한다. 이 명령들은 `svc-infra`
Bash 안이 아니라 sudo 권한이 있는 관리 계정 셸에서 실행한다.

```bash
sudo apt-get update
RCLONE_DEB=/tmp/rclone-v1.75.1-linux-amd64.deb
curl -fL -o "$RCLONE_DEB" \
  https://github.com/rclone/rclone/releases/download/v1.75.1/rclone-v1.75.1-linux-amd64.deb
echo '09c9f7606ed9e31eecc1eec26a89992cf2931a8d2d1a5f0ae2bb1c11630ffb15  /tmp/rclone-v1.75.1-linux-amd64.deb' \
  | sha256sum -c -
sudo apt-get install "$RCLONE_DEB"
unset RCLONE_DEB
sudo install -d -o 1001 -g 1001 -m 0750 \
  /persist/srv/seaweedfs/master \
  /persist/srv/seaweedfs/volume-ssd \
  /persist/srv/seaweedfs/filer \
  /persist/srv/seaweedfs/migration-reports
sudo loginctl enable-linger svc-infra
sudo -u svc-infra -H \
  env XDG_RUNTIME_DIR="/run/user/$(id -u svc-infra)" \
  /bin/bash
cd /opt/infra
```

Quadlet은 SeaweedFS 4.41 multi-architecture manifest-list digest
`sha256:43b768cd62b00d132439cda881b93fd1adebf1b315e996e794087743821d771d`에
고정되어 있다. 이 digest로 pull에 성공해야 하며, 이 서버의 `x86_64`/`linux/amd64`
platform manifest digest도 확인한다.

```bash
podman pull docker.io/chrislusf/seaweedfs@sha256:43b768cd62b00d132439cda881b93fd1adebf1b315e996e794087743821d771d
podman image inspect --format '{{index .RepoDigests 0}}' \
  docker.io/chrislusf/seaweedfs@sha256:43b768cd62b00d132439cda881b93fd1adebf1b315e996e794087743821d771d
```

Podman은 pull 입력에 사용한 manifest-list digest가 아니라 선택된 platform manifest
digest를 `RepoDigests`에 표시한다. 따라서 이 amd64 서버에서 기대하는 출력은
`docker.io/chrislusf/seaweedfs@sha256:3bbe24f6d5f5818327adcfeda7d85240ed53212dab05f91af14484c6446ec5eb`이다.
pull 출력의 `8da20b...` 같은 값은 image configuration ID이므로 이 검증의 비교 대상이
아니다. 다른 platform digest가 나오거나 digest-pinned pull 자체가 실패하면 중단한다.

## 2. credential과 S3 IAM secret

`[OPERATOR: svc-infra]` 아래 블록은 0600 임시 디렉터리에서 키를 만들고 Podman secret에
저장한다. Git 저장소에는 값이 기록되지 않는다. 기존 secret이 있으면 명령이 실패하며
덮어쓰지 않는다.

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

`podman secret ls`에 9개 이름이 보여야 한다. secret 값을 화면이나 로그로 출력하지 않는다.

## 3. preflight, Quadlet 설치, 시작

`[OPERATOR: svc-infra]` host에 `rclone` 1.75.1 이상, `jq`, `curl`, Python 3가 있어야 한다.
Ubuntu 저장소의 1.60.1-DEV는 SeaweedFS 4.41의 S3 PUT과 호환되지 않으므로 사용하지 않는다.
1절에서 checksum을 검증한 공식 rclone package를 설치한 뒤 명시적으로 연 `svc-infra`
Bash에서 계속한다.

```bash
just seaweed preflight
```

기대 결과는 source/가용 byte와 현재 RUNNING run ID JSON, 마지막 `OK`이다. source 기준값은
약 28 GB / 52,015 files이며 현저한 차이는 조사한다. 특수 파일, F1 API 실패, 부족한 공간
중 하나라도 보고되면 중단한다.

Quadlet을 설치한다. 이 서버의 `~/.config/containers/systemd`는 이미
`/opt/infra/quadlet`을 가리키므로 파일을 다시 `install`하지 않는다. 아래 확인 결과가
`/opt/infra/quadlet`이 아니면 중단하고 배치 방식을 먼저 확인한다. F1 파일과 staged
template은 이 단계에서 별도로 설치하거나 활성 파일로 교체하지 않는다.

```bash
test "$(readlink -f ~/.config/containers/systemd)" = /opt/infra/quadlet
systemctl --user daemon-reload
systemctl --user start seaweed-master.service seaweed-volume-ssd.service seaweed-filer.service seaweed-s3.service
systemctl --user --no-pager --full status seaweed-master.service seaweed-volume-ssd.service seaweed-filer.service seaweed-s3.service
```

모두 `active (running)`/healthy여야 하며 `ss -ltn '( sport = :9000 )'`에는
`127.0.0.1:9000`만 보여야 한다. 9333/8080/8888은 host listener가 없어야 한다.

```bash
just seaweed bootstrap-buckets
just seaweed status
curl -fsS http://127.0.0.1:9000/ >/dev/null && echo 'ERROR anonymous access' || echo 'OK anonymous denied'
```

세 bucket과 cross-bucket deny가 확인되어야 한다. 재시작 후 persistence도 확인한다.

```bash
systemctl --user restart seaweed-master.service seaweed-volume-ssd.service seaweed-filer.service seaweed-s3.service
just seaweed status
```

## 4. F2 즉시 전환

`[AGENT]` 저장소의 F2 Quadlet은 잘못된 F1 mount를 제거하고 전용 S3 destination과 secret을
사용하도록 변경되어 있다. MLflow image에는 `boto3`가 포함된다.

`[OPERATOR: svc-infra]` 적용 직전에 F2 artifact 경로가 4 KB/0 files 수준이고 F2 API의 run
수가 0인지 다시 확인한다. 하나라도 run/artifact가 있으면 중단해 별도 이관한다. F1 health와
RUNNING ID를 기록한 뒤 다음을 수행한다.

```bash
du -sh /persist/srv/mlflow/mlflow_f2
find /persist/srv/mlflow/mlflow_f2 -type f -print
curl -fsS http://127.0.0.1:5001/mlflow-f1/health
test "$(readlink -f ~/.config/containers/systemd)" = /opt/infra/quadlet
systemctl --user daemon-reload
systemctl --user start mlflow-postgres-build.service
systemctl --user restart mlflow_f2.service
curl -fsS http://127.0.0.1:5002/mlflow-f2/health
just seaweed smoke-f2
```

smoke는 `migration-smoke` experiment에 1 MiB sentinel을 올리고 다시 받아 SHA-256을 비교해
성공 종료한다. run은 감사 기록으로 유지한다. 전후 F1 health, endpoint 설정 및 RUNNING run
ID가 동일해야 한다. F1 Quadlet은 이 단계에서 설치하거나 수정하지 않는다.

## 5. F1 완료 run 사전 복사

`[OPERATOR: svc-infra]` 시작 순간 API에서 종료 상태인 run만 allowlist에 고정된다. 이후 새
run과 RUNNING run은 제외된다. 명령에는 `ionice -c3`, `nice -n19`, 25 MiB/s, transfers/checkers
각 2가 고정되어 있다.

```bash
just seaweed copy-f1-completed
just seaweed verify-f1-completed
```

검증 성공 출력에는 SHA-256/count/bytes 일치와 report 경로가 나온다. 보고서의 `runs.json`에
제외된 RUNNING ID가 있고, `rclone-verify.log`에 missing/different/error가 없어야 한다.
보고서는 `/persist/srv/seaweedfs/migration-reports/`에 남으며 credential은 포함하지 않는다.

## 6. 향후 F1 컷오버

진입 조건은 (1) artifact producer 정지 또는 upload 차단, (2) RUNNING run 0, (3) source
manifest를 두 번 생성해 동일함이다. `copy-f1-final`은 두 manifest를 연속 생성해 비교한 뒤,
동일할 때만 전체 source를 복사한다.

```bash
just seaweed copy-f1-final
just seaweed verify-f1-final
```

verify가 성공한 뒤에만 `[OPERATOR: svc-infra]`가 현재 F1 Quadlet을 읽어 운영 보고서 경로에
백업한다. `/opt/infra/quadlet`은 repository owner 소유이므로 `svc-infra`가 파일을 덮어쓰지
않는다.

```bash
cp /opt/infra/quadlet/mlflow_f1.container \
  /persist/srv/seaweedfs/migration-reports/mlflow_f1.container.pre-seaweed
```

`[AGENT/repository owner]`가 staged 내용을 active Quadlet에 반영하고 정적 생성 검증을 한다.

```bash
install -m 0644 quadlet/mlflow_f1-s3.container.template \
  quadlet/mlflow_f1.container
```

반영 완료 후 `[OPERATOR: svc-infra]`만 user service를 reload/restart한다.

```bash
systemctl --user daemon-reload
systemctl --user restart mlflow_f1.service
curl -fsS http://127.0.0.1:5001/mlflow-f1/health
just seaweed verify-f1-existing
just seaweed smoke-f1
```

기존 완료 run 하나의 artifact 다운로드와 새 smoke upload/download를 검증한다. local 원본은
삭제하지 않는다.

## 7. rollback

SeaweedFS 적용 전에는 F2 Quadlet 백업을 복원하면 된다. S3 전환 뒤 새 artifact가 하나라도
기록됐다면 단순히 local 설정으로 되돌리지 않는다. 먼저 writer를 멈추고, S3에서 local로
비파괴 역복사하고 SHA-256/count/bytes를 검증한 뒤에만 설정을 복원한다. 이 역이관은 본
runbook에 자동 삭제/덮어쓰기 명령으로 제공하지 않으며 별도 승인과 백업 계획이 필요하다.

장애 시에도 source 또는 target 삭제, `rclone sync`, overwrite 옵션을 사용하지 않는다.

## 8. Tailnet S3 HTTPS 및 Presigned Multipart 운영

외부 분석 장비가 Tailnet을 통해 대용량 artifact를 병렬 chunk로 직접 업로드·다운로드할 수 있도록
Tailnet 전용 S3 HTTPS endpoint를 운영한다.

- **외부 S3 엔드포인트**: `https://esillileu-server.tail4941d3.ts.net:9000`
- **로컬 바인딩**: `127.0.0.1:9000` (Tailscale Serve가 `100.89.18.91:9000`에서 종단)
- **MLflow Quadlet 환경변수**:
  - `Environment=MLFLOW_S3_ENDPOINT_URL=https://esillileu-server.tail4941d3.ts.net:9000`
  - `Environment=MLFLOW_BOTO_CLIENT_ADDRESSING_STYLE=path`

### 8.1 Tailscale Serve 명령

```bash
# 활성화 (기존 443, 5432 설정 보존)
sudo tailscale serve --bg --https=9000 http://127.0.0.1:9000

# 비활성화 (롤백 시)
sudo tailscale serve --https=9000 off
```

### 8.2 검증 명령

```bash
# S3 호환성, 서명(SigV4), Presigned GET/PUT/Multipart 전수 검증
just seaweed test-s3-endpoint https://esillileu-server.tail4941d3.ts.net:9000

# MLflow F1/F2 15 MiB multipart upload/download smoke 검증
just seaweed smoke-multipart-f1
just seaweed smoke-multipart-f2
```

### 8.3 외부 분석 장비 설정

외부 장비에서 대용량 artifact 직접 multipart 전송을 활성화하려면:

```bash
export MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD=true
export MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD=true
```

문제가 발생할 경우 환경변수를 `false`로 되돌리면 즉시 기존 MLflow proxy streaming 경로로 복귀한다.

