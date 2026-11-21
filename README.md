# 짝짜꿍 Backend

FastAPI 서버와 도메인 로직을 구현합니다. AWS에서 Docker로 실행하고 Vercel의 Flutter Web이 HTTPS로 호출합니다.

| 담당 | 브랜치 | 경로 |
| --- | --- | --- |
| 박승원 | `screen-photo` | `app/photo/` |
| 이성찬 | `voice` | `app/voice/` |
| 안홍모 | `reconcile` | `app/reconcile/` |
| 구본혁 | `server` | `app/api/`, `app/workflow/`, 서버 진입점·의존성·Docker |
| 우영 | `integration-notes` | `docs/contract/`, `docs/integration/` |

공통 입출력은 [계약 문서](docs/contract/v1.md)를 따릅니다. 우영은 웹·서버·DB 각각의 커밋과 실제 배포 URL을 기록합니다.

## 협업 순서

역할 브랜치에서 변경하고 이 저장소의 `main`을 대상으로 PR을 엽니다. PR에 직접 확인한 동작과 결과, 다음 담당자가 받을 값을 적습니다. 우영이 검토·병합하고 구본혁이 병합된 버전을 배포합니다. 팀 배포팩의 `frontend/`, `backend/`는 해당 저장소 루트를 뜻합니다. DB 변경은 Database 저장소로 보냅니다.

[팀 노션과 역할 카드](https://tasteful-fight-7cd.notion.site/41d58907758380bdb941e7781b7096af) · [Database](https://github.com/JJak-JJak-GGeung/JJak-JJak-GGeung-Database) · [Frontend](https://github.com/JJak-JJak-GGeung/JJak-JJak-GGeung-Frontend)

공개 A 자료는 팀 배포팩에서 사용합니다. 로컬 환경 파일·인증 키·B 입력과 정답표는 팀 저장소 밖에 보관합니다.
