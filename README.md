# SCHUTZ Management Worker

Prometheus·Loki의 관측 결과를 수집해 사건을 기록하고 AI 분석 및 Slack 알림 작업을 처리하는 백그라운드 프로그램입니다. 이 저장소에는 Worker 코드, 공통 DB 모듈, Dockerfile과 CI/CD가 포함되어 있습니다.

## 코드와 실행 진입점

| 경로 | 역할 |
|---|---|
| `management/worker.py` | 주기적 수집·점검, `python -m management.worker` 진입점 |
| `management/jobs.py` | AI 분석과 Slack 알림 작업 |
| `management/alerting.py`, `management/alert_policy.py` | Prometheus 경보 상태와 처리 정책 |
| `management/history.py` | AI 입력에 참고할 과거 사건·조치 선택 |
| `management/domain.py`, `management/models.py` | 사건 처리와 관리 DB 모델 |
| `management/config.py` | `.env`, `.management.env` 기반 Worker 설정 |
| `portal/db.py`, `portal/models.py` | DB 연결 및 공통 모델 |
| `Dockerfile`, `requirements.txt` | 이미지 빌드와 Python 의존성 |
| `deploy/` | 현재 저장소의 자동 배포와 복구 스크립트 |

저장소 루트의 이미지 빌드 명령입니다.

```bash
docker build -t schutz-management-worker:ci .
```

기본 실행 명령은 `python -m management.worker`입니다. 실행에는 DB와 관측 대상 및 외부 연동 설정이 필요합니다. 테스트 실행과 실제 Worker 시작은 구분합니다. Slack·AI의 외부 호출 설정과 대기 작업을 확인하지 않은 상태에서 운영 Worker를 임의로 시작하지 않습니다.

## 문서 안내

| 주제 | 대표 문서 |
|---|---|
| 공통 경보·복구·알림 계약 | [Worker 중심 경보 통합](docs/worker-alert-integration.md) |
| AI 관측 범위 | [수집 실패·서비스 중단 구분](docs/ai-scrape-observation-scope.md) |
| 과거 이력 입력 | [과거 장애·조치 이력 활용](docs/ai-history-context.md) |

경보 통합 문서는 Web·Worker 공통 계약의 대표 문서입니다. Web 화면에서의 접수·조치·종료 및 UI 검수 이력은 [Web 저장소 문서](https://github.com/schutz-project/management-web/tree/main/docs)를 참조합니다. 각 문서의 테스트 수·검증 범위·배포 대기 문구는 작성 당시 기록입니다.

## 검증과 배포

`.github/workflows/ci.yml`에 배포 검사, 이미지 빌드·import 및 AI·경보 관련 시험이 정의되어 있습니다. 기존 main push 및 수동 실행에는 조건에 따라 ECR 게시·EC2 배포를 수행하는 job도 있습니다. 파일 업로드나 병합이 서버에 영향을 주지 않는다고 가정하지 않습니다.

Web 저장소와 내용이 같은 `deploy/service-release.sh`도 이 저장소의 `deploy/auto-release.py`가 직접 읽으므로 유지합니다. 다른 저장소에 사본이 있다는 이유만으로 삭제하지 않습니다.

## 관련 저장소와 비밀정보

- [management-web](https://github.com/schutz-project/management-web): 관리자 조회·사건 처리 화면
- [fast-api](https://github.com/schutz-project/fast-api): 관측 대상 직원 포털
- [infra](https://github.com/schutz-project/infra): 배포 설정·모니터링 구성·운영 기록

DB 비밀번호, Slack Webhook, AI API 키 및 실제 환경 파일은 커밋하지 않습니다.
