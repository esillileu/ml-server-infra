# infra

단일 서버의 rootless Podman/Quadlet 서비스와 reverse SSH link를 관리하는 저장소다.
운영 컨테이너와 user systemd manager의 소유자는 비로그인 서비스 계정
`svc-infra`(UID/GID 1001)다.

## 운영 계정

`svc-infra`에는 로그인 셸을 전제로 한 `sudo -iu`를 사용하지 않는다. 관리 셸은 다음처럼
연다.

```bash
sudo -u svc-infra -H \
  env XDG_RUNTIME_DIR="/run/user/$(id -u svc-infra)" \
  /bin/bash
cd /opt/infra
```

한 명령만 실행할 때도 runtime directory를 동적으로 지정한다.

```bash
sudo -u svc-infra -H \
  env XDG_RUNTIME_DIR="/run/user/$(id -u svc-infra)" \
  just --justfile /opt/infra/justfile --list
```

`loginctl enable-linger svc-infra`는 `svc-infra`의 user manager를 로그인과 독립적으로
유지한다. 이 명령 자체는 이 저장소의 Quadlet 서비스를 enable하지 않는다. 단, 이미
enable된 user unit이 있다면 user manager 시작 시 자동 기동될 수 있다.

`~/.config/containers/systemd`는 `/opt/infra/quadlet`을 가리키는 symlink다. Quadlet 원본은
repository owner가 수정하고, `svc-infra` 운영자는 `systemctl --user daemon-reload`와
서비스 start/restart를 수행한다.

## 현재 구성

| 서비스 | 로컬 endpoint | 저장소/역할 |
|---|---|---|
| MLflow F1 | `http://127.0.0.1:5001/mlflow-f1` | `s3://mlflow-f1-artifacts` |
| MLflow F2 | `http://127.0.0.1:5002/mlflow-f2` | `s3://mlflow-f2-artifacts` |
| PostgreSQL | `127.0.0.1:5432` | MLflow backend DB 및 프로젝트 DB |
| SeaweedFS S3 | `http://127.0.0.1:9000` | MLflow artifact와 F2 corpus |

SeaweedFS는 `master`, `volume`, `filer`, `s3`의 네 Quadlet 서비스로 분리되어 있다.
master/filer metadata는 `/persist/srv/seaweedfs`에 영속화되고, bulk volume 데이터는
ext4 HDD의 `/data/srv/seaweedfs/volume`에 영속화된다. S3 gateway만 host loopback의
9000번 포트에 공개되며 관리 포트는 host에 공개하지 않는다.

버킷과 credential은 용도별로 분리한다.

- `mlflow-f1-artifacts`: MLflow F1 전용
- `mlflow-f2-artifacts`: MLflow F2 전용
- `f2-corpus`: F2 corpus 전용

credential과 SeaweedFS IAM JSON은 Git 파일이 아니라 `svc-infra`의 Podman secret으로
관리한다.

### 파일시스템 레이아웃 및 아카이브 상태
- **Active state**:
  - `/persist/srv/seaweedfs/master`: Master metadata
  - `/persist/srv/seaweedfs/filer`: Filer metadata
  - `/data/srv/seaweedfs/volume`: Bulk volume storage (HDD)
- **Inactive / Archive & Rollback (active 코드 및 서비스에서 참조 금지)**:
  - `/persist/srv/seaweedfs/archive/f1-local-to-s3`: 과거 F1 S3 마이그레이션 보고서 보관소
  - `/persist/srv/seaweedfs/rollback/volume-<UTC_TIMESTAMP>`: cutover 직전 old SSD volume rollback copy
  - `/persist/srv/mlflow/archive/f1-pre-s3`: 과거 F1 로컬 파일시스템 원본 artifact 보관소

### 이력 메모 (Historical Note)
- **F1 local filesystem → S3 migration**: 완료 (전체 artifact S3 보관 및 검증 완료)
- **SeaweedFS bulk volume SSD → `/data` HDD migration**: 완료 (Quadlet `seaweed-volume`으로 중립화 및 ext4 HDD 이관 완료)

## 상태 확인

```bash
cd /opt/infra
just seaweed status
systemctl --user --no-pager --full status \
  seaweed-master.service seaweed-volume.service \
  seaweed-filer.service seaweed-s3.service \
  mlflow_f1.service mlflow_f2.service
```

SeaweedFS 네 컨테이너는 `healthy`, F1/F2 endpoint는 `UP`이어야 한다. S3 anonymous 요청은
거부되는 것이 정상이다.

주요 검증 명령은 다음과 같다.

```bash
just seaweed verify-f1-known-good
just seaweed smoke-f1
just seaweed smoke-f2
just seaweed smoke-multipart-f1
just seaweed smoke-multipart-f2
just seaweed test-s3-endpoint
```

## `svc-ln` reverse link

`svc-ln`은 `181` 대상(`user3@163.152.23.181`)으로 하나의 SSH 연결을 만들고, 서비스별
remote loopback 포트를 이 서버의 local endpoint로 전달한다.

| 서비스 | 이 서버 | `181` 서버에서 접근할 주소 |
|---|---|---|
| MLflow F1 | `127.0.0.1:5001` | `127.0.0.1:15001` |
| MLflow F2 | `127.0.0.1:5002` | `127.0.0.1:15002` |
| PostgreSQL | `127.0.0.1:5432` | `127.0.0.1:15432` |
| SeaweedFS S3 | `127.0.0.1:9000` | `127.0.0.1:19000` |

배포는 symlink와 user unit만 설치하며 tunnel을 enable/start하지 않는다.

```bash
just svc-ln-deploy
svc-ln on 181
svc-ln status 181
```

설정 변경 후 실행 중인 SSH 연결에 새 forward를 반영하려면 명시적으로 재시작한다.

```bash
svc-ln restart 181
```

S3 reverse link는 `181` 서버에서 확인한다.

```bash
curl -fsS http://127.0.0.1:19000/healthz
```

모든 remote listener는 `127.0.0.1` 전용이다. `0.0.0.0` 또는 `::`에 노출되면 운영을
중단하고 remote sshd의 `GatewayPorts` 설정을 확인한다. 자세한 배포와 장애 진단은
[`svc-ln/README.md`](svc-ln/README.md)를 참고한다.

## PostgreSQL 프로젝트 관리

사용 가능한 recipe를 확인한다.

```bash
just --list psql
just --list psql project
```

프로젝트 역할과 DB 생성, 비밀번호 변경, 삭제는 다음과 같다.

```bash
just psql project create PROJECT_NAME
just psql project change-pass PROJECT_NAME
just psql project drop PROJECT_NAME
```

`drop`은 파괴적 작업이므로 대상 프로젝트 이름과 백업 여부를 먼저 확인한다. PostgreSQL에
직접 접속하거나 `psql` 인자를 넘길 수도 있다.

```bash
just psql
just psql default -c '\l'
```

## 변경 적용 원칙

- 에이전트/repository owner는 설정과 스크립트를 작성하고 정적 검증한다.
- `svc-infra` 운영자는 Podman pull/secret, Quadlet reload, 서비스 start/restart를 수행한다.
- secret 값, IAM JSON, 임시 rclone config와 migration payload는 Git에 저장하지 않는다.
- active storage relocation, archive 삭제, `mlflow gc`, rollback은 별도 검토와 승인 후 수행한다.
