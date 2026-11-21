"""POST /api/transcriptions — 구본혁 서버에 붙이는 라우터.

    from app.voice.router import router as voice_router
    app.include_router(voice_router, dependencies=[Depends(점주_인증)])

요청: multipart/form-data, 필드 file 한 개 (브라우저가 녹음한 파일·MIME 그대로)
응답: 200 {"draft_text": "..."}
오류: 4xx/5xx {"detail": {"code": "...", "message": "..."}}
      → 화면은 편집란을 직접 입력 가능한 상태로 유지
이 경로는 아무것도 저장하지 않는다. 메모 수·input_revision 불변.
"""

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from .transcribe import MAX_BYTES, TranscriptionError, transcribe_audio

router = APIRouter(tags=["voice"])


@router.post("/api/transcriptions")
async def create_transcription(file: UploadFile = File(...)) -> dict:
    data = await file.read(MAX_BYTES + 1)
    try:
        text = await run_in_threadpool(transcribe_audio, data, file.content_type)
    except TranscriptionError as e:
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})
    finally:
        del data  # 원음 미보관
    return {"draft_text": text}
