"""녹음 바이트 + MIME → OpenAI 전사 → 점주가 고칠 draft_text.

원칙 (공통 입출력 v1 · 판단 규칙 R04)
- 원음은 메모리에서 전사에만 쓰고 디스크·DB·Storage에 저장하지 않는다.
- 기대 문장(정답)을 프롬프트로 넣지 않는다.
- 초안은 정산에 반영하지 않는다. 저장은 점주 확인 후 메모 API(구본혁)가 한다.
"""

from __future__ import annotations

import os

# OpenAI 전사 업로드 한도
MAX_BYTES = 25 * 1024 * 1024
# 이보다 작으면 사실상 빈 녹음(헤더만 있는 파일)
MIN_BYTES = 1024

# 2026-11-21 A06 실제 녹음(m4a)·합성 음성 비교에서 원문과 완전 일치. 대안: gpt-4o-transcribe
DEFAULT_MODEL = "gpt-transcribe"

# 브라우저·기기가 보내는 MIME → OpenAI가 받는 확장자
_MIME_EXT = {
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mp4": "mp4",
    "video/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}


class TranscriptionError(Exception):
    """화면은 code와 무관하게 편집란을 직접 입력 가능한 상태로 둔다."""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def _sniff_ext(data: bytes) -> str | None:
    """MIME이 비었거나 octet-stream일 때 파일 머리로 형식을 판별한다."""
    head = data[:16]
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    return None


def resolve_extension(mime: str | None, data: bytes) -> str:
    base = (mime or "").split(";", 1)[0].strip().lower()
    ext = _MIME_EXT.get(base)
    if ext:
        return ext
    if base in ("", "application/octet-stream"):
        ext = _sniff_ext(data)
        if ext:
            return ext
    raise TranscriptionError(
        "UNSUPPORTED_AUDIO", 415, f"지원하지 않는 오디오 형식입니다: {base or '알 수 없음'}"
    )


def _default_client():
    if not os.environ.get("OPENAI_API_KEY"):
        raise TranscriptionError("NO_API_KEY", 503, "서버에 OPENAI_API_KEY가 설정되지 않았습니다.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise TranscriptionError(
            "NO_OPENAI_SDK", 503, "서버에 openai 패키지가 없습니다. requirements.txt에 openai를 추가해 주세요."
        ) from exc

    return OpenAI(timeout=60.0, max_retries=1)


def transcribe_audio(data: bytes, mime: str | None, *, client=None) -> str:
    """오디오 바이트와 MIME을 받아 편집 가능한 전사 초안 문자열을 돌려준다."""
    if not data or len(data) < MIN_BYTES:
        raise TranscriptionError("EMPTY_AUDIO", 400, "녹음이 비어 있습니다. 다시 녹음하거나 직접 입력해 주세요.")
    if len(data) > MAX_BYTES:
        raise TranscriptionError("AUDIO_TOO_LARGE", 413, "녹음이 25MB를 넘습니다. 짧게 나눠 녹음해 주세요.")

    ext = resolve_extension(mime, data)
    client = client or _default_client()
    model = os.environ.get("OPENAI_TRANSCRIBE_MODEL", DEFAULT_MODEL)
    content_type = (mime or "").split(";", 1)[0].strip() or "application/octet-stream"

    try:
        result = client.audio.transcriptions.create(
            model=model,
            file=(f"recording.{ext}", data, content_type),
            language="ko",
        )
    except TranscriptionError:
        raise
    except Exception as exc:  # 인증·한도·네트워크 오류 모두 화면에서는 직접 입력으로 이어진다
        raise TranscriptionError(
            "TRANSCRIPTION_FAILED", 502, f"전사 호출 실패: {type(exc).__name__}: {exc}"
        ) from exc

    text = result if isinstance(result, str) else getattr(result, "text", "")
    return (text or "").strip()
