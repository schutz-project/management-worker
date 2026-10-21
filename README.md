# SCHUTZ Management Worker

SCHUTZ 프로젝트의 장애 감지 및 사건 처리 백그라운드 프로그램입니다.

## 주요 기능

- Prometheus를 통한 서비스 상태 점검 결과 확인
- Loki를 통한 로그 조회 및 분석
- 장애 발생 시 사건(Incident) 기록 및 상태 관리
- AI를 활용한 장애 분석 작업
- Slack Webhook을 이용한 알림 처리

## 주요 파일

| 파일 | 설명 |
|---|---|
| `management/worker.py` | Worker 실행 및 주기적 점검 |
| `management/jobs.py` | AI 분석 및 Slack 알림 작업 |
| `management/domain.py` | 사건 기록 및 처리 로직 |
| `management/models.py` | 관리 시스템 데이터베이스 모델 |
| `management/config.py` | 환경변수 기반 설정 |
| `management/observability.py` | 모니터링 관련 보조 기능 |
| `portal/db.py` | 공통 데이터베이스 연결 |
| `portal/models.py` | 공통 데이터베이스 모델 |
| `requirements.txt` | Python 라이브러리 목록 |

## 기존 AWS 실행 방식

```bash
python -m management.worker
```

기존 AWS 모니터링 서버의 Docker 컨테이너에서 확인한 실행 명령입니다.

## 보안 주의사항

- `.env` 및 실제 비밀번호가 포함된 설정 파일은 GitHub에 업로드하지 않습니다.
- Slack Webhook과 AI API 키는 환경변수로 관리합니다.
- 데이터베이스 접속 정보는 별도로 관리합니다.

## 참고

이 저장소는 기존 AWS 환경에서 사용하던 Worker 코드를 분리한 작업본입니다.

단독 실행 및 배포에 필요한 설정은 추후 검증해야 하며, GitHub에 코드를 업로드해도 AWS 서버에 자동 반영되지는 않습니다.