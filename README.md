# 개요

Incident Tracker는 Prometheus/Grafana 기반 모니터링 환경에서 알람 인시던트의 수명주기를 추적하는 경량 서비스. 알람의 발생부터 인지, 조사, 해결까지의 전 과정을 타임라인 형태로 기록하고, 사후 보고서를 Markdown으로 내보내는 기능을 제공.

## 배경

기존 모니터링 스택(Prometheus, Grafana, Alertmanager)은 알람의 실시간 전달에 특화되어 있으나, 다음과 같은 운영 정보가 부재.

- 알람 발생 후 담당자가 언제 인지했는지
- 자동 해소와 수동 해결의 구분
- 인시던트별 조치 내역 및 원인 기록
- 과거 인시던트 이력 조회 및 통계

Dooray 웹훅을 통한 알람 수신만으로는 "이 알람이 언제 해결되었고, 누가 대응했는지"를 추적할 수 없는 상황.

## 대안 검토

| 도구 | 유형 | 검토 결과 |
|------|------|----------|
| Grafana Alert State History | 내장 기능 | 상태 변경 이력만 조회 가능. 인지 시점 기록, 메모, 보고서 기능 없음 |
| Keep | 오픈소스 IRM | AIOps 기반 통합 플랫폼. 풍부한 기능이나 Docker Compose 기준 10개 이상의 컨테이너 필요. 단일 VM 환경에 과도 |
| Grafana OnCall | 오픈소스 IRM | 온콜 스케줄 관리 중심. 별도 DB(PostgreSQL), Redis, Celery 등 부수 인프라 필요 |
| GoAlert | 오픈소스 | 온콜 로테이션 및 에스컬레이션 특화. 인시던트 타임라인 기록 기능 부재 |

기존 모니터링 VM에 부수 인프라 없이 추가 가능하고, 인시던트 타임라인 기록이라는 핵심 요구에 집중한 경량 서비스가 필요하여 자체 구현을 선택.

## 선택 근거

- 단일 VM(monitoring-vm)에 Docker Compose 서비스 하나로 배포 가능
- SQLite 사용으로 별도 DB 서버 불필요
- Alertmanager/Grafana의 기존 webhook 경로에 수신기만 추가하는 방식으로 기존 알람 흐름에 영향 없음
- 필요한 기능만 구현하여 운영 복잡도 최소화

## 지원 기능

### 알람 수신 및 인시던트 생성

- Alertmanager webhook (`POST /webhook/alertmanager`)
- Grafana Unified Alerting webhook (`POST /webhook/grafana`)
- fingerprint 기반 firing/resolved 자동 매칭
- 동일 fingerprint의 중복 인시던트 생성 방지

### 인시던트 수명주기 관리

인시던트 상태 전이 흐름:

```
firing -> acknowledged -> investigating -> resolved
```

| 상태 | 설명 |
|------|------|
| firing | 알람 수신, 아직 미인지 |
| acknowledged | 담당자가 인지 완료 |
| investigating | 원인 조사 착수 |
| resolved | 해결 완료 (자동 또는 수동) |

- 원클릭 확인(acknowledged) 기능으로 빠른 인지 기록
- 수동 해결 시 원인/조치 메모 필수 입력
- 자동 해소(Alertmanager/Grafana resolved 신호)와 수동 해결 구분 기록

### 타임라인 기록

모든 상태 변경 및 메모를 시간순으로 기록.

| 이벤트 유형 | 설명 |
|-------------|------|
| alert_fired | 알람 발생 (자동) |
| acknowledged | 담당자 인지 |
| status_change | 상태 전환 |
| memo | 자유 메모 |
| alert_resolved | 자동 해소 |

- 메모 추가/삭제 (시스템 이벤트는 삭제 불가)
- 각 타임라인 이벤트에 자유 텍스트 기록 가능

### 필터링 및 조회

- 알람명별 필터링 (드롭다운 선택)
- 심각도별 필터링 (critical, warning, info)
- 날짜 범위 필터링 (date picker)
- 해결된 인시던트 페이지네이션 (20건 단위)
- 활성 인시던트 경과 시간 실시간 표시 ("N분 전", "N시간 전")

### Labels 표시

Alertmanager/Grafana에서 전달받은 라벨(instance, job, namespace 등)을 인시던트 카드 및 상세 페이지에 태그로 표시.

### 문서 내보내기

- 단일 인시던트 Markdown 리포트 (`GET /api/incidents/{id}/export`)
  - 발생/인지/해소 시각, 소요 시간, 타임라인 포함
- 기간별 요약 리포트 (`GET /api/export?start=YYYY-MM-DD&end=YYYY-MM-DD`)
  - 총 건수, 해결/미해결 수, 평균 인지 시간(MTTA), 평균 해결 시간(MTTR)

### 자동 새로고침

메인 페이지 30초 간격 자동 갱신. 메모 입력 중에는 새로고침을 건너뛰어 사용자 입력 보호.

## 아키텍처

### 시스템 구성

```
Prometheus Alerting Rules
        |
        v
  Alertmanager ──webhook──> Incident Tracker (FastAPI + SQLite)
        |                          |
        v                          v
  Dooray Webhook            Web UI (/tracker/)
                                   |
Grafana Unified Alerting           v
        |                   nginx (reverse proxy, HTTPS)
        └──webhook────────────────>|
```

- Alertmanager: `webhook_configs`에 Incident Tracker 엔드포인트 추가 (`send_resolved: true`)
- Grafana: Contact Point로 Incident Tracker webhook 등록, Notification Policy에서 `continue: true`로 기존 Dooray 경로와 병행
- nginx: `/tracker/` 경로를 Incident Tracker 서비스로 리버스 프록시

### 기술 스택

| 구성 요소 | 기술 | 선택 이유 |
|-----------|------|----------|
| 애플리케이션 | FastAPI (Python 3.12) | 비동기 지원, 자동 API 문서, 빠른 개발 |
| 데이터베이스 | SQLite (WAL 모드) | 별도 서버 불필요, 단일 파일 백업 용이 |
| 템플릿 | Jinja2 | FastAPI 내장 지원, 서버 사이드 렌더링 |
| 컨테이너 | Docker (python:3.12-alpine) | 경량 이미지, 기존 Docker Compose에 통합 |
| 리버스 프록시 | nginx | 기존 Grafana용 nginx에 location 블록 추가 |

### 데이터 모델

```
incidents
  - id (PK)
  - fingerprint        알람 식별자 (label set 해시)
  - alert_name         알람 이름
  - severity           심각도 (critical/warning/info)
  - status             현재 상태
  - fired_at           발생 시각
  - acknowledged_at    인지 시각
  - resolved_at        해소 시각
  - resolution_type    해결 방식 (auto/manual)
  - labels             전체 라벨 (JSON)
  - created_at         레코드 생성 시각

timeline_events
  - id (PK)
  - incident_id (FK)   소속 인시던트
  - event_type         이벤트 유형
  - old_status         변경 전 상태
  - new_status         변경 후 상태
  - note               메모 텍스트
  - created_at         이벤트 시각
```

### API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/webhook/alertmanager` | Alertmanager webhook 수신 |
| POST | `/webhook/grafana` | Grafana webhook 수신 |
| POST | `/api/incidents/{id}/ack` | 원클릭 확인 |
| PATCH | `/api/incidents/{id}` | 상태 변경 |
| POST | `/api/incidents/{id}/memo` | 메모 추가 |
| DELETE | `/api/timeline/{id}` | 메모 삭제 |
| GET | `/api/incidents` | 인시던트 목록 |
| GET | `/api/incidents/{id}` | 인시던트 상세 |
| GET | `/api/incidents/{id}/export` | 단일 리포트 |
| GET | `/api/export` | 기간별 요약 리포트 |
| GET | `/` | 웹 UI 메인 |
| GET | `/incidents/{id}` | 웹 UI 상세 |

### 배포 구성

Docker Compose 서비스로 기존 monitoring-vm에 통합:

```yaml
incident-tracker:
  build: ./incident-tracker
  container_name: incident-tracker
  restart: unless-stopped
  environment:
    - DB_PATH=/data/incidents.db
    - PORT=8001
    - BASE_PATH=/tracker
  volumes:
    - incident-data:/data
  networks:
    - monitoring
```

nginx location 블록:

```nginx
location /tracker/ {
    proxy_pass http://incident-tracker:8001/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### 접근 경로

- 웹 UI: `https://<monitoring-vm-ip>/tracker/`
- API: `https://<monitoring-vm-ip>/tracker/api/...`

## 파일 구조

```
monitoring-vm/incident-tracker/
  app.py              애플리케이션 메인 (API, webhook, DB, 내보내기)
  templates/
    index.html        메인 목록 뷰 (필터, 페이지네이션, 자동 갱신)
    detail.html       인시던트 상세 뷰 (타임라인, 액션)
  requirements.txt    Python 의존성
  Dockerfile          컨테이너 빌드 정의
```
