# 팀 작업 지침

역할 브랜치에서 변경하고 해당 저장소의 `main`으로 PR을 엽니다. PR에는 직접 확인한 동작과 결과, 다음 담당자가 받을 값을 적습니다. 우영이 검토·병합하고 구본혁이 병합 버전을 배포합니다. 팀 배포팩의 `frontend/`, `backend/`는 각 저장소 루트를 뜻합니다. DB 변경은 Database 저장소로 보냅니다.

| 담당 | 브랜치 | 경로 |
| --- | --- | --- |
| 박승원 | `screen-photo` | Frontend `lib/` 및 Backend `app/photo/` |
| 이성찬 | `voice` | Backend `app/voice/` |
| 안홍모 | `reconcile` | Backend `app/reconcile/` |
| 구본혁 | `server` | Backend `app/api/`·`app/workflow/`·배포 설정, Database `supabase/migrations/` |
| 우영 | `integration-notes` | Backend `docs/contract/`·`docs/integration/` |

우영은 웹·서버·DB의 배포 커밋과 실제 배포 주소를 기록합니다. 로컬 환경 파일·인증 키·B 입력과 정답표는 저장소 밖에 둡니다.

[팀 노션과 역할 카드](https://tasteful-fight-7cd.notion.site/41d58907758380bdb941e7781b7096af) · [배포 검수 기록](./integration/README.md)
