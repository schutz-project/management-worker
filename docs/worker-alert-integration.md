# Worker 중심 경보 통합

## 구성과 범위

Prometheus는 기존 경보 규칙을 평가하고, Worker가 `/api/v1/rules?type=alert`를 조회해 사건·AI·Slack 처리를 담당합니다. 별도 Alertmanager·컨테이너·외부 수신 포트는 추가하지 않습니다. 기존 Prometheus 규칙과 임계치는 변경하지 않습니다.

공식 API: https://prometheus.io/docs/prometheus/latest/querying/api/#rules

| 경보 | 처리 |
|---|---|
| PortalProbeFailed | 기존 Worker probe 탐지 유지. 이 경보로 사건을 다시 만들지 않음 |
| MonitoringTargetDown | 수집 대상별 사건 등록 |
| PortalTelemetryMissing | 앱 지표 누락 사건 등록 |
| PortalHighServerErrorRate | 기존 5xx 비율 규칙의 firing을 사건 등록 |

Node Exporter·별도 OTel Collector 기능의 Alloy 통합, Grafana 중지는 이전 작업입니다. 별도 Blackbox의 Alloy 내장 기능 전환은 이번 PR에 포함하지 않습니다. FastAPI OTel SDK·Prometheus·Loki·업무 DB·관리 DB는 각 역할이 있어 유지합니다.

## 상태·중복·복구

- firing만 사건을 만들고 pending은 사건을 만들지 않습니다.
- 알려진 경보와 job·instance·check만 식별자로 사용합니다. annotation·임의 라벨·쿼리는 저장하거나 실행하지 않습니다.
- 경보+대상 지문으로 열린 사건과 최초 Slack·AI 작업을 재사용합니다. 상태는 재시작 후에도 유지합니다.
- 규칙 평가 오류·오래된 평가·응답 실패·규칙 누락은 미확인으로 처리합니다. 경보가 사라졌다는 이유만으로 복구하지 않습니다.
- 수집 대상 복구는 해당 `up=1`과 최신 원본 표본 시각, 지표 누락 복구는 최신 앱 지표, 5xx 복구는 최신 앱 지표·양수 요청량·기존 임계치 이하 오류 비율을 확인합니다.
- 서로 다른 최신 평가에서 정상 지표가 두 번 확인되면 사건당 복구 Slack 작업을 한 번 준비합니다. 발송 직전에도 상태를 재확인합니다. 재발·미확인이면 발송을 보류합니다.
- 사건은 자동 종료하지 않습니다. 관리자 조치 기록과 최신 복구 조건이 있어야 종료할 수 있습니다. 현재 새 UI의 조회 전용 정책은 변경하지 않습니다.
- Slack 타임아웃·전송 결과 불명·실행 중 작업 lease 만료는 자동 재전송을 멈추고 DELIVERY_UNKNOWN으로 남깁니다. 실제 수신 여부 확인 후 수동 처리가 필요합니다. 외부 webhook까지 엄밀한 exactly-once 전송을 보장하지는 않습니다.
- 반복·재발 알림은 열린 사건당 최초/AI완료/복구 각 한 번입니다. 사건을 종료한 뒤 다시 firing되면 새 사건이 생성됩니다. 재알림 주기·묶음 알림·업무시간 정책은 이번 범위가 아닙니다.

## DB와 설정

관리 DB에 `prometheus_alert_states` 테이블 하나를 추가합니다. 기존 테이블·업무 데이터는 변경하지 않습니다. Worker 시작 시 checkfirst 방식으로 이 테이블과 해당 인덱스만 생성합니다. DB 계정에 생성 권한이 필요하며 DDL 실패 시 새 Worker가 시작하지 않으므로 기존 배포 건강 검사·롤백 경로로 확인해야 합니다.

`PROMETHEUS_ALERTS_ENABLED` 기본값은 true입니다. false는 신규 규칙 수집을 중단합니다. 이미 만들어진 초기 Slack·AI 작업을 취소하는 설정은 아닙니다. 배포되면 기존 firing 경보도 사건·현재 설정된 Slack/AI mode에 따라 처리됩니다. 테스트에서는 외부 발송을 하지 않습니다.

Worker 자체나 Prometheus 전체가 중단된 상태를 이 경로만으로 외부에 통지할 수는 없습니다. 이는 별도 외부 생존 감시 범위입니다.

## 검증

Worker: 경보 통합 18개 + 기존 AI 제한시간 4개, 총 22개. 중복·재시작·대상 분리·보류·복구·재발·누락·오류·안전한 라벨·전송 불명 처리를 검사합니다. Web: 공통 종료 정책 3개, UI 35개. 로컬 Python 검증은 SQLAlchemy 2.0 및 SQLite를 사용했고, CI는 저장소 의존성의 Docker 이미지에서 재검증합니다. 운영 PostgreSQL·Prometheus 실제 payload와 Slack 발송 실증은 승인 후 단계입니다.

## 배포와 복구

1. Worker PR과 Web 동반 PR의 CI를 확인합니다. Worker·Web 현재 실행 이미지 태그와 MANAGE_PUBLIC_URL이 실제 관리자 HTTPS 주소인지 확인합니다.
2. Worker 먼저 배포해 추가 테이블을 준비하고 수집·기존 probe/로그 처리가 유지되는지 확인합니다.
3. Web을 배포해 경보 근거·대상·복구 상태·종료 조건을 연결합니다. 새 UI 디자인은 유지합니다.
4. 먼저 모의 클라이언트/시험 환경으로 검증하고 운영에서 무분별한 재실행·기존 사건 재알림은 하지 않습니다.
5. 원상복구는 Worker 이전 이미지 → Web 이전 이미지 순서로 진행합니다. 추가 테이블과 생성된 사건·근거는 삭제하지 않습니다. 이전 Worker는 slack_recovery 작업을 실행하지 않으며, 신규 경보 사건은 이전 Web에서 종료 조건을 충족하지 못할 수 있습니다. 복구 중에도 기존 업무 서비스·DB·HTTPS 설정은 유지합니다.

코드 복구 기준 브랜치: `checkpoint/pre-alert-integration-20261026`

- Worker: `0fa276d7384610520e3f81a63362034e0eb58794`
- Web: `0cbc0a3a12801ca248892c2fe24221ce63c66fd4`

병합·배포는 사용자 승인 이후에 진행합니다. 이번 PR 작성 중 실제 서버·Slack·AI는 실행하지 않았습니다.
