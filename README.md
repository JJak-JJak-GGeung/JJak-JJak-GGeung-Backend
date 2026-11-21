# 짝짜꿍 Backend

식당 점주가 식자재 대금을 보내기 전에 **납품명세 · 점포 수령기록 · 청구서**를 대조하고 근거와 함께 직접 승인하도록 돕는 서비스의 서버입니다.

**🔗 서비스: https://jjak-jjak-ggeung-frontend.vercel.app** · 체험 방법과 예시 자료는 [Frontend README](https://github.com/JJak-JJak-GGeung/JJak-JJak-GGeung-Frontend#2분-체험)에 있습니다.
**API:** `https://jjak-jjak-ggeung-a1.rkrbhm0ryexya.ap-northeast-2.cs.amazonlightsail.com` (FastAPI · Docker · AWS Lightsail)

| 저장소 | 내용 |
| --- | --- |
| [Frontend](https://github.com/JJak-JJak-GGeung/JJak-JJak-GGeung-Frontend) | Flutter Web 점주 화면 · 예시 자료 |
| **Backend (여기)** | FastAPI · 사진 판독 · 음성 전사 · 대조 계산 |
| [Database](https://github.com/JJak-JJak-GGeung/JJak-JJak-GGeung-Database) | Supabase PostgreSQL 스키마 · 승인 트랜잭션 |

## 설계 원칙 · AI는 읽고, 규칙은 계산하고, 결정은 점주가 합니다

```
사진 ──▶ [AI 비전 판독] ──▶ 판독 초안 ──(점주 확인)──▶ 원본·행 저장 ─┐
                                                                    ├─▶ [규칙 대조] ─▶ 차이·금액·승인 가능 여부
음성 ──▶ [AI 음성 전사] ──▶ 메모 초안 ──(점주 확인)──▶ 통화 메모 저장 ─┘                    │
                                                                         (점주 직접 승인)
                                                                                 ▼
                                                                  승인본 스냅샷 (당시 근거·금액·버전 고정)
```

- **AI는 제안만 합니다.** 판독값과 전사 초안은 점주가 확인하기 전까지 정산에 반영되지 않습니다.
- **돈과 관련된 판단은 규칙으로 합니다.** 같은 입력에는 같은 결과가 나오고, 근거가 빠지면 금액을 빈 값으로 두고 승인을 막습니다.
- **같은 요청은 한 번만 반영됩니다.** 재시도·연속 클릭은 같은 ID를 돌려주고, 같은 키에 다른 본문은 `409 KEY_REUSE`입니다.
- **원음과 비밀은 남기지 않습니다.** 녹음은 전사에만 쓰고 저장하지 않습니다.

## 모듈

| 경로 | 역할 | AI / 규칙 |
| --- | --- | --- |
| [`app/photo/`](app/photo) | 거래 문서 사진 → 문서 종류·거래처·명세 번호·품목·수량·단가·원본 위치 | OpenAI 비전 (`gpt-4.1-mini`, 구조화 출력) |
| [`app/voice/`](app/voice) | 점주 통화 메모 녹음 → 편집 가능한 초안 `draft_text` | OpenAI 음성 전사 (`gpt-transcribe`) |
| [`app/reconcile/`](app/reconcile) | 명세·청구·수령 대조, 차이·확인할 금액, 수정 청구 대체, 승인 조건 | 규칙 (결정적 계산) |
| `app/workflow/` | 근거 변경 시 해당 정산 재대조 | 규칙 |
| `app/api/` | FastAPI 진입점, 브라우저 세션, 사진 확인·저장, 승인·재조회 | — |

## 주요 API

| 메서드 · 경로 | 설명 |
| --- | --- |
| `POST /api/photo-previews` → `/confirm` | 사진 판독 미리보기 → 점주가 확인·수정한 값만 저장 |
| `GET /api/periods/{id}` | 주간 정산: 합계, 거래별 차이, 승인 차단 사유, 근거 원본 |
| `POST /api/periods/{id}/confirmations` | 수령 줄 ↔ 납품 연결, 수정 청구 대체 확인, 행 정정 |
| `POST /api/transcriptions` | 녹음 파일 → `{"draft_text"}` (저장 없음) |
| `POST /api/transactions/{id}/memos` | 점주가 확인한 통화 메모 저장 (`VOICE_MEMO` / `TYPED_TEXT`) |
| `POST /api/periods/{id}/approvals` | 현재 자료 버전에서 직접 승인 → 승인본 스냅샷 |
| `GET /api/periods/{id}/approvals`, `GET /api/approvals/{id}` | 승인 내역과 당시 근거 재조회 |
| `GET /api/documents/{id}/original` | 비공개 원본 사진 열기 |
| `GET /api/sample-deliveries` | 체험용 합성 명세표 |

회원가입 없이 브라우저가 만든 무작위 키(`Authorization: Bearer device-v1.…`)로 자기 점포 자료만 조회합니다. 전체 계약은 [`docs/contract/v1.md`](docs/contract/v1.md)를 참고하세요.

## 실행

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # SUPABASE_URL, SUPABASE_SECRET_KEY, OPENAI_API_KEY, CORS_ORIGINS(프런트 주소)
.venv/bin/uvicorn app.api.main:app --reload
.venv/bin/python -m pytest
```

Docker: `docker build -t jjak-backend . && docker run -p 8000:8000 --env-file .env jjak-backend`

---

<details>
<summary>개발 기록 (팀 내부 협업 규칙)</summary>

FastAPI 서버와 도메인 로직을 구현합니다. AWS에서 Docker로 실행하고 Vercel의 Flutter Web이 HTTPS로 호출합니다.

### GPT 사진 분석 전환 브랜치

`feat/gpt-document-extraction`에서는 OCR 대신 GPT 이미지 분석을 사용합니다.
루트 `.env`의 `OPENAI_API_KEY`를 백엔드에서 읽으며, 원본과 편집 표의 사람 검수 후 저장합니다.
[설정·검수 화면·새 API·DB 선행 조건](app/photo/README.md)을 확인하세요.
기존 무검수 업로드 경로는 409를 반환하므로 프론트엔드도 새 검수 API에 연결해야 합니다.

| 담당 | 브랜치 | 경로 |
| --- | --- | --- |
| 박승원 | `screen-photo` | `app/photo/` |
| 이성찬 | `voice` | `app/voice/` |
| 안홍모 | `reconcile` | `app/reconcile/` |
| 구본혁 | `server` | `app/api/`, `app/workflow/`, 서버 진입점·의존성·Docker |
| 우영 | `integration-notes` | `docs/contract/`, `docs/integration/` |

공통 입출력은 [계약 문서](docs/contract/v1.md)를 따릅니다. 우영은 웹·서버·DB 각각의 커밋과 실제 배포 URL을 기록합니다.

역할 브랜치에서 변경하고 이 저장소의 `main`을 대상으로 PR을 엽니다. PR에 직접 확인한 동작과 결과, 다음 담당자가 받을 값을 적습니다. 우영이 검토·병합하고 구본혁이 병합된 버전을 배포합니다. 팀 배포팩의 `frontend/`, `backend/`는 해당 저장소 루트를 뜻합니다. DB 변경은 Database 저장소로 보냅니다.

[팀 노션과 역할 카드](https://tasteful-fight-7cd.notion.site/41d58907758380bdb941e7781b7096af)

공개 A 자료는 팀 배포팩에서 사용합니다. 로컬 환경 파일·인증 키·B 입력과 정답표는 팀 저장소 밖에 보관합니다.

</details>
