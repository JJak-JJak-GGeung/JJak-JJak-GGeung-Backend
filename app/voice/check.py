"""배포 서버 대상 음성 경로 검증 (역할 카드 B1·B2·C1·C2).

배포 주소·점주 토큰·거래 ID만 .env에 채우면 바로 실행된다.

    VOICE_CHECK_BASE=https://api.example.com     # 구본혁 배포 주소
    VOICE_CHECK_TOKEN=eyJ...                     # 점주 세션 접근 토큰 (Authorization: Bearer)
    VOICE_CHECK_TX=<transaction_id>              # B2·C1·C2 대상 거래 (A: DL-A131)
    VOICE_CHECK_PERIOD=<period_id>               # 선택: 거래 응답에 input_revision이 없을 때

    python -m app.voice.check b1 녹음.m4a          # 배포 전사 vs 로컬 전사
    python -m app.voice.check b2 녹음.m4a          # 전사 전후 메모 수·버전 불변
    python -m app.voice.check c1 --write [--text "점주가 고친 문장"]   # 확인 저장·재시도 같은 ID
    python -m app.voice.check c2 --write [--text "직접 입력 문장"]     # TYPED_TEXT 같은 메모 경로

c1·c2는 실제 DB에 메모를 남기므로 --write가 있어야 실행한다.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx


@dataclass
class Report:
    step: str
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # (PASS/FAIL/INFO, 항목, 값)

    def add(self, ok: bool | None, item: str, value) -> None:
        mark = "INFO" if ok is None else ("PASS" if ok else "FAIL")
        self.rows.append((mark, item, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)))

    @property
    def passed(self) -> bool:
        return all(m != "FAIL" for m, _, _ in self.rows)

    def print(self) -> None:
        print(f"\n== {self.step} · {'통과' if self.passed else '실패'}")
        for m, item, v in self.rows:
            print(f"[{m}] {item}: {v}")


def _mime_for(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return r.text[:300]


def _tx_state(api: httpx.Client, tx: str, period: str | None) -> dict:
    """거래의 메모 목록과 input_revision을 읽는다."""
    r = api.get(f"/api/transactions/{tx}")
    r.raise_for_status()
    body = r.json()
    memos = body.get("memos") or []
    rev = body.get("input_revision")
    if rev is None and period:
        pr = api.get(f"/api/periods/{period}")
        pr.raise_for_status()
        rev = pr.json().get("input_revision")
    return {"memos": memos, "memo_count": len(memos), "input_revision": rev}


def transcribe_remote(api: httpx.Client, audio: bytes, mime: str, name: str):
    t0 = time.perf_counter()
    r = api.post("/api/transcriptions", files={"file": (name, audio, mime)})
    return r, int((time.perf_counter() - t0) * 1000)


def check_b1(api: httpx.Client, audio_path: Path, local_text: str | None) -> Report:
    rep = Report("B1 배포 API 전사")
    audio, mime = audio_path.read_bytes(), _mime_for(audio_path)
    rep.add(None, "입력", f"{audio_path.name} · {len(audio)} bytes · {mime}")
    r, ms = transcribe_remote(api, audio, mime, audio_path.name)
    body = _json(r)
    rep.add(r.status_code == 200, "HTTP", f"{r.status_code} · {ms}ms")
    ok_shape = isinstance(body, dict) and set(body) == {"draft_text"}
    rep.add(ok_shape, "응답 필드는 draft_text 하나", body)
    if local_text is not None and ok_shape:
        rep.add(body["draft_text"] == local_text, "로컬 모듈 결과와 동일", local_text)
    return rep


def check_b2(api: httpx.Client, audio_path: Path, tx: str, period: str | None) -> Report:
    rep = Report("B2 초안 단계 미저장 (A-08)")
    before = _tx_state(api, tx, period)
    rep.add(None, "전사 전", {"memo_count": before["memo_count"], "input_revision": before["input_revision"]})
    r, _ = transcribe_remote(api, audio_path.read_bytes(), _mime_for(audio_path), audio_path.name)
    rep.add(r.status_code == 200, "전사 응답", _json(r))
    after = _tx_state(api, tx, period)
    rep.add(after["memo_count"] == before["memo_count"], "메모 수 불변", f"{before['memo_count']} → {after['memo_count']}")
    rep.add(
        after["input_revision"] == before["input_revision"],
        "input_revision 불변",
        f"{before['input_revision']} → {after['input_revision']}",
    )
    return rep


def check_memo_save(
    api: httpx.Client, tx: str, period: str | None, text: str, source_mode: str, step: str
) -> Report:
    """C1(VOICE_MEMO)·C2(TYPED_TEXT) 공통: 확인 저장, 같은 키 재시도, 같은 키 다른 본문."""
    rep = Report(step)
    before = _tx_state(api, tx, period)
    key = f"voice-check-{uuid.uuid4()}"
    body = {
        "confirmed_text": text,
        "source_mode": source_mode,
        "expected_revision": before["input_revision"],
        "request_key": key,
    }
    rep.add(None, "요청", body)

    r1 = api.post(f"/api/transactions/{tx}/memos", json=body)
    b1 = _json(r1)
    rep.add(r1.is_success, "첫 저장", f"{r1.status_code} {json.dumps(b1, ensure_ascii=False)}")
    if not r1.is_success:
        return rep
    memo_id = b1.get("memo_id")
    rep.add(bool(memo_id), "memo_id 반환", memo_id)
    rep.add(str(b1.get("transaction_id")) == str(tx), "transaction_id 일치", b1.get("transaction_id"))
    rep.add("input_revision" in b1, "input_revision 반환", b1.get("input_revision"))

    r2 = api.post(f"/api/transactions/{tx}/memos", json=body)
    b2 = _json(r2)
    rep.add(r2.is_success and b2.get("memo_id") == memo_id, "같은 키 재시도 → 같은 memo_id", f"{r2.status_code} {b2}")

    after = _tx_state(api, tx, period)
    rep.add(after["memo_count"] == before["memo_count"] + 1, "메모 정확히 1건 추가", f"{before['memo_count']} → {after['memo_count']}")
    saved = next((m for m in after["memos"] if m.get("memo_id") == memo_id), None)
    rep.add(saved is not None, "거래 재조회에 메모 존재", saved)
    if saved:
        rep.add(saved.get("confirmed_text") == text, "확인한 문장 그대로 저장", saved.get("confirmed_text"))
        rep.add(saved.get("source_mode") == source_mode, "source_mode", saved.get("source_mode"))

    r3 = api.post(f"/api/transactions/{tx}/memos", json={**body, "confirmed_text": text + " (다른 본문)"})
    rep.add(r3.status_code == 409, "같은 키 다른 본문 → 409 KEY_REUSE", f"{r3.status_code} {_json(r3)}")
    final = _tx_state(api, tx, period)
    rep.add(final["memo_count"] == after["memo_count"], "충돌 요청은 저장 안 됨", final["memo_count"])
    return rep


def _load_env() -> None:
    from .__main__ import _load_dotenv

    _load_dotenv()


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    _load_env()
    p = argparse.ArgumentParser(prog="python -m app.voice.check")
    p.add_argument("step", choices=["b1", "b2", "c1", "c2"])
    p.add_argument("audio", nargs="?", help="b1·b2 녹음 파일")
    p.add_argument("--text", help="c1·c2 저장할 확인 문장")
    p.add_argument("--write", action="store_true", help="c1·c2 실제 DB 저장 허용")
    p.add_argument("--no-local", action="store_true", help="b1에서 로컬 전사 비교 생략")
    a = p.parse_args(argv)

    base = os.environ.get("VOICE_CHECK_BASE", "").rstrip("/")
    token = os.environ.get("VOICE_CHECK_TOKEN", "")
    tx = os.environ.get("VOICE_CHECK_TX", "")
    period = os.environ.get("VOICE_CHECK_PERIOD") or None
    missing = [k for k, v in [("VOICE_CHECK_BASE", base), ("VOICE_CHECK_TOKEN", token)] if not v]
    if a.step != "b1" and not tx:
        missing.append("VOICE_CHECK_TX")
    if a.step in ("b1", "b2") and not a.audio:
        missing.append("녹음 파일 인자")
    if missing:
        print("미설정: " + ", ".join(missing))
        return 2
    if a.step in ("c1", "c2") and not a.write:
        print("c1·c2는 실제 메모를 저장합니다. 확인했으면 --write를 붙여 다시 실행하세요.")
        return 2

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base, headers=headers, timeout=60) as api:
        if a.step == "b1":
            local = None
            if not a.no_local:
                from .transcribe import transcribe_audio

                path = Path(a.audio)
                local = transcribe_audio(path.read_bytes(), _mime_for(path))
            rep = check_b1(api, Path(a.audio), local)
        elif a.step == "b2":
            rep = check_b2(api, Path(a.audio), tx, period)
        elif a.step == "c1":
            text = a.text or "거래처는 11월 13일에 장어 40킬로를 다 보냈다고 합니다. 우리 가게 기록에는 36킬로입니다. 거래처에 다시 확인 요청함."
            rep = check_memo_save(api, tx, period, text, "VOICE_MEMO", "C1 확인 저장·재시도 (A-09)")
        else:
            text = a.text or "마이크 없이 직접 입력: 거래처는 40킬로를 보냈다고 하고 우리 기록은 36킬로입니다."
            rep = check_memo_save(api, tx, period, text, "TYPED_TEXT", "C2 직접 입력 같은 메모 경로")
    rep.print()
    print(f"\n대상 {base} · 거래 {tx or '-'}")
    return 0 if rep.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
