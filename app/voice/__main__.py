"""단독 확인: python -m app.voice <녹음파일> [MIME]

저장소 루트의 .env에서 OPENAI_API_KEY를 읽는다(이미 환경변수가 있으면 그대로 사용).
"""

import mimetypes
import sys
from pathlib import Path

from .transcribe import TranscriptionError, transcribe_audio


def _load_dotenv() -> None:
    import os

    env = Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("사용법: python -m app.voice <녹음파일> [MIME]")
        return 2
    _load_dotenv()
    path = Path(sys.argv[1])
    mime = sys.argv[2] if len(sys.argv) > 2 else (mimetypes.guess_type(path.name)[0] or "")
    data = path.read_bytes()
    print(f"입력: {path.name} · {len(data)} bytes · MIME {mime or '(없음)'}")
    try:
        print("draft_text:", transcribe_audio(data, mime))
    except TranscriptionError as e:
        print(f"오류 {e.status} {e.code}: {e.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
