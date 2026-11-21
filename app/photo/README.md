# GPT 사진 분석과 사람 검수

별도 OCR을 사용하지 않는다. `extract_document`는 OpenAI Responses API에 원본 이미지를 보내
문서 종류와 품목 필드를 Structured Outputs로 받는다. 결과는 미확인 초안이며 계산·합의·승인은 수행하지 않는다.

## 실행

저장소 루트의 `.env`를 읽는다. 이미 설정된 프로세스 환경변수가 우선한다.
`.env`는 커밋하거나 프론트엔드에 전달하지 않는다.

```dotenv
OPENAI_API_KEY=본인_API_키
# 선택: 기본 모델은 gpt-4.1-mini-2025-04-14. 실제 문서 정확도는 별도 평가한다.
OPENAI_DOCUMENT_MODEL=gpt-4.1-mini-2025-04-14
SUPABASE_URL=본인_Supabase_URL
SUPABASE_SECRET_KEY=서버_전용_키
FRONTEND_ORIGIN=https://프론트엔드_주소
# 선택: 미설정 시 SUPABASE_SECRET_KEY를 초안 서명에도 사용한다.
PHOTO_REVIEW_SIGNING_KEY=충분히_긴_서버_전용_임의값
```

```powershell
python -m pip install -r requirements-dev.txt
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

`http://127.0.0.1:8000/review`에서 점포·거래처·기간을 선택하고 사진 분석 → 원본과 표 비교 →
문서 종류/필드 수정·행 추가/삭제 → 검수자 입력 → 확인 저장을 진행한다.
확인 저장은 DB 마이그레이션 적용 후 가능하다. Flutter 기존 업로드 버튼은 아래 새 API에 연결해야 한다.
이 브랜치의 `/review`는 백엔드가 제공하는 검수 화면이며 별도 Flutter 저장소를 변경하지 않았다.

분석만 실행해 JSON을 확인하려면:

```powershell
python -m app.photo "docs/team-work-pack/A02_납품명세_1113.png" --output "docs/A02_gpt_reading.json"
```

A01~A05는 이미지, A06은 음성용 TXT이므로 이미지 분석에 넣지 않는다.
JSON/로그에는 요청 ID·모델·프롬프트 버전·토큰 사용량·시간이 포함된다. 키와 공급자 오류 본문은 기록하지 않는다.

## API 계약

- `POST /api/photo-previews-v2`: multipart `file`. 반환: `draft`, `signature`, `issued_at`, `requires_review=true`.
  DB/Storage에 쓰지 않는다. 초안 서명은 파일 해시와 원추출본을 묶고 1시간 후 만료된다.
- `POST /api/photo-previews-v2/confirm`: 같은 `file`, `draft_json`, `signature`, `issued_at`,
  `review_json`, `store_id`, `supplier_id`, `period_id`, `expected_revision`, `request_key`, `actor_label`.
  review_json은 `doc_type`, 모든 문서 머리글 필드, `lines`를 포함한다. 각 행에는 원본 순서인
  `source_row`(1부터 시작) 또는 사람이 추가한 행의 `null`과 기존 행 필드를 넣는다.
  삭제 행은 배열에서 제외한다. 원추출본은 변경하지 않는다. UNKNOWN은 검수로 분류하기 전 저장 불가.
- 성공: `document_id`, `period_id`, `duplicate`, `input_revision`.
  같은 키·같은 본문은 기존 ID, 키 재사용/구버전/이미 등록된 사진의 다른 검수는 409.
  통신 실패 시 최초 본문과 키를 그대로 재전송한다. 저장된 문서의 재분석으로 확인본을 덮지 않는다.
- 기존 `/api/periods/{id}/documents` 자동 저장은 409 `PHOTO_REVIEW_REQUIRED`로 막는다.
  기존 성공 추출 재조회는 유지하고 미완료 추출의 무검수 재실행도 막는다.

문서 필드: `supplier_name_read`, `doc_no`, `doc_date`, `period_start`, `period_end`,
`payment_due_on`, `replaces_doc_no`, `agreement_text` (string/null).
행 필드: `delivery_no`, `delivery_date`, `item`, `spec`, `unit`, `qty_read`, `unit_price_read`,
`amount_read`, `tax_amount_read`, `line_date_text`, `item_code`, `origin`, `lot_no`, `line_note`,
`supply_amount_read`, `discount_amount_read`, `line_total_read`. 수량·단가는 십진 문자열이며, 금액은 원 단위 정수 또는 null이다.
추가 필드를 검수 요청에서 생략하면 서명된 판독값을 유지한다. 값을 명시적으로 null로 보내면 빈칸으로 저장한다.
배포 시 Database의 `20261121055610_document_line_details.sql`을 먼저 적용한다.
부호 있는 값은 보존하되 대조 엔진의 기존 승인 차단 규칙을 따른다.

`source_location`은 페이지/행 순서만 제공하고 정확한 좌표는 `null`이다.
사람이 추가한 행은 원본 행 좌표를 만들어 내지 않는다.
사진에 적힌 합의 문구를 읽어도 실제 합의 확인이 생성되지는 않는다.
수령 연결·수정청구 대체/합의와 정산 승인은 기존 API에서 별도로 수행한다.

## DB 인계와 배포

`migrations/20261121_confirm_photo_review_v2.sql`은 Database 저장소로 인계할 변경이다.
Database main만으로 부족하며 `db-workflow-bridge`의 `20261121020330_workflow_api_bridge.sql`
(read_period_state/persist_extraction/publish_reconciliation) 적용이 선행돼야 한다.
이 파일은 원추출본과 검수본·검수자·시각을 metadata.photo_review에 함께 보존하고 한 트랜잭션에서 저장한다.
DB 적용·운영 배포는 이 브랜치 작성 과정에서 수행하지 않는다. 기존 등록 문서는 자동 재추출/덮어쓰기하지 않는다.
현재 프로젝트의 점포/사용자 인증 체계는 기존 서버와 동일하다. actor_label은 표시명이며 인증 증거가 아니다.

Docker 빌드에서 RapidOCR/ONNX/OpenCV 설치나 모델 다운로드는 하지 않는다.
키는 런타임에 `docker run --env-file .env -p 8000:8000 IMAGE` 등으로 주입한다.
기준 서버/음성 파일은 backend main `8584d4494f9fc0a92ccecc3e21344410c5c1573a`에서 가져왔다.
다른 `feat/vision-document-extraction` 브랜치의 OCR 보조 호출 코드는 사용하지 않았다.

## 검증

```powershell
python -m unittest discover -v
```

모의 GPT 응답 테스트와 실제 사진 정확도 평가는 별개다. 원본 A01~A05의 문서 번호·행 수·수량·단가·금액을
먼저 비교하고 다양한 실제 양식에서 무수정 통과율·검수 시간·지연·토큰 비용을 측정한다.
Structured Outputs는 형식만 제한하며 숫자의 사실 정확도를 보장하지 않는다.

공식 문서: https://developers.openai.com/api/docs/guides/images-vision ·
https://developers.openai.com/api/docs/guides/structured-outputs

### ?? main? ??

?? `/api/photo-previews`? `/confirm`? ??? ????? ??????? ?? ??? ????.
? ??? `vision.extract_document` ?? ???? ?? ??? GPT ?? ???? ???? OCR fallback? ??.
? `/api/photo-previews-v2`? ?????? ????? ??/????? ??? ????? ??? ????.
?? v2? ?? ??????? ???? ?? ?????? ? ??? ???? ???.
?? ?? ???/??? ?? ???? ?? ?? ??? ?? ??409? ????.
`trade_date`, `issued_on`, `quoted_on`, `source_date_text`, `store_name_read`, `tax_amount_read`, `line_date_text`? ????.
?? ?? ?? ? OCR? ???? ?? ???? ??? ????.
