# app/voice · 음성 전사 (담당 이성찬)

점주가 통화 내용을 말하면 **편집 가능한 초안(draft_text)만** 돌려준다.
저장은 하지 않는다. 확인 저장은 `POST /api/transactions/{T}/memos`(구본혁) 경로로, 직접 입력과 같은 입력란·저장 버튼을 쓴다.

## 서버 연결 (구본혁)

```python
from fastapi import Depends
from app.voice.router import router as voice_router

app.include_router(voice_router, dependencies=[Depends(점주_인증)])
```

- 의존성: `app/voice/requirements.txt` (openai, fastapi, python-multipart)
- 환경변수: `OPENAI_API_KEY` (필수), `OPENAI_TRANSCRIBE_MODEL` (선택, 기본 `gpt-transcribe`, 대안 `gpt-4o-transcribe`)
- 원음은 메모리에서만 쓰고 디스크·DB·Storage에 두지 않는다. 메모 수·input_revision 불변.

## 계약

```
POST /api/transcriptions
Content-Type: multipart/form-data
file = 브라우저가 녹음한 파일 1개 (MIME 그대로: audio/webm;codecs=opus, audio/mp4 등)

200 {"draft_text": "거래처는 11월 13일에 장어 40킬로를 다 보냈다고 합니다. ..."}
```

| HTTP | detail.code | 의미 | 화면 동작 |
|---|---|---|---|
| 400 | EMPTY_AUDIO | 녹음이 비었음 | 다시 녹음 또는 직접 입력 |
| 413 | AUDIO_TOO_LARGE | 25MB 초과 | 짧게 다시 녹음 또는 직접 입력 |
| 415 | UNSUPPORTED_AUDIO | 오디오 형식 아님 | 직접 입력 |
| 502 | TRANSCRIPTION_FAILED | OpenAI 호출 실패(한도·네트워크) | 직접 입력 |
| 503 | NO_API_KEY | 서버 키 미설정 | 직접 입력 |

오류 본문: `{"detail": {"code": "...", "message": "한국어 안내"}}` — 어떤 오류든 편집란은 직접 입력 가능 상태로 유지.

MIME이 비었거나 `application/octet-stream`이어도 파일 머리(webm/ogg/mp4/wav/mp3/flac)로 판별한다.

## 화면 연결 (박승원)

- 마이크 → 녹음 파일을 `file` 필드로 보낸다. 가능하면 녹음기의 실제 MIME을 `contentType`에 넣는다.
- 응답 `draft_text`를 답변 입력란에 채운다. 이 시점엔 아무것도 저장되지 않는다.
- 점주가 고친 뒤 확인 저장 → 메모 API에 `confirmed_text`, `source_mode=VOICE_MEMO`(직접 입력은 `TYPED_TEXT`), `expected_revision`, `request_key`.
- 마이크 거절·전사 실패 시 작성 중인 거래와 입력값 유지.

## 단독 확인

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r app/voice/requirements.txt pytest httpx
.venv/Scripts/python -m pytest app/voice -q          # 가짜 클라이언트 13건
.venv/Scripts/python -m app.voice 녹음.m4a           # 실제 전사 (.env의 OPENAI_API_KEY 사용)
curl -F "file=@녹음.webm;type=audio/webm" http://localhost:8000/api/transcriptions
```
