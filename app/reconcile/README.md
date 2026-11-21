# A2 계산 호출

```python
from app.reconcile import reconcile_single_line, reconcile_lines

reconcile_single_line("40", "36", "20000")
# delta_qty="4", check_amount=80000, match_result="MISMATCH", can_approve=False

reconcile_lines([
    {"transaction_id": "example-1", "billed_qty": "40", "received_qty": "36",
     "unit_price": "20000", "unit": "kg", "units_comparable": True},
    {"transaction_id": "example-2", "billed_qty": "60", "received_qty": None,
     "unit_price": "20000", "unit": "kg", "units_comparable": True},
])
# items[0]의 4kg/80000원은 유지. summary의 received_qty/delta_qty/check_amount=None.
# approvable_amount=None, approved_amount=None, can_approve=False.
```

- 수량·단가는 십진 문자열, 기재 청구액 `billed_amount`는 원 단위 정수다. 모름은 `None`, 확인한 0은 `"0"`이다.
- 단일 행 함수의 기존 3개 인자는 **같은 확인 단위로 환산된 수량과 해당 단위당 단가**를 전제로 한다. 확인할 수 없으면 `units_comparable=False`를 보낸다.
- 여러 행 함수에는 `units_comparable=True`를 명시해야 비교한다. 원본 단위가 다르면 호출자가 확인한 환산 근거로 먼저 변환하고 원문 단위·근거를 별도 키에 보존한다. 함수가 환산율을 추측하지 않는다.
- `lines`는 호출자가 같은 점포·거래처·기간에서 이미 연결한 전체 청구 대상 행이어야 한다. 누락 수령 행을 입력에서 제외하면 안 된다. 확인 수령 연결·중복 제외는 아래 B1 함수를 이용한다.
- ID·원문 단위·`evidence_refs` 등 입력 메타데이터는 행별로 보존한다. 서로 다른 기준 단위의 수량은 합산하지 않는다.
- `billed_amount`는 기재 금액을 합산하며 수량×단가로 새로 만들지 않는다. 원 미만 차액은 반올림 규칙이 없어 `None`으로 반환한다.
- 이 함수는 A2 계산 결과 조각이다. 전체 API 응답, 주간 근거 기반 승인 허용, 승인 저장은 아직 구현하지 않았다. 일치해도 승인은 차단하며 승인 대상액·저장 승인액은 모두 `None`이다.

기준: [Notion A2 역할 카드](https://app.notion.com/p/169552b817178217bbe801046fdcdd8a), [공통 입출력 v1](https://app.notion.com/p/86d552b8171783e7913381f3e02eaea9).

검산: `python -m unittest app.reconcile.test_reconcile -v`

## B1 · 확인 수령 연결

```python
from app.reconcile.matching import reconcile_receipts

scope = {"store_id": "example-store", "supplier_id": "example-supplier",
         "period_id": "example-period"}
result = reconcile_receipts(
    [{**scope, "line_id": "delivery-1", "delivery_no": "example-number",
      "delivery_date": "2026-11-13", "item": "eel", "spec": "cleaned",
      "unit": "kg", "declared_qty": "40", "billed_qty": "40",
      "unit_price": "20000", "billed_amount": 800000, "evidence_refs": []}],
    [{**scope, "receipt_id": "receipt-1", "source_line_id": "source-1",
      "delivery_no": "example-number", "received_qty": "36", "unit": "kg",
      "confirmed_by": "example-owner", "confirmed_at": "2026-11-21T10:00:00+09:00",
      "evidence_refs": []}],
    **scope,
)
# receipt_links[0]: LINKED / target_delivery_line_id=delivery-1
# items[0]: received_qty="36", delta_qty="4", check_amount=80000
```

입력은 **서버가 인증·확인 이력을 검증한** 수령 이벤트와 현재 유효 청구가 붙은 납품 행 전체다. 이 함수는 입력 문자열만으로 점주 본인 여부를 인증하지 않는다. 판독만 된 행을 `confirmed_by/at`으로 임의 확정하면 안 된다. 실제 사진 판독·점주 확인·청구/납품 원본 정규화 연동은 별도 작업이다.

- 모든 납품 행의 점포·거래처·기간과 `line_id`를 검증한다. 다른 범위의 수령이나 확인자/시각이 없는 수령은 집계하지 않는다.
- 같은 범위의 미확인 수령도 `receipt_links[].candidates`로 후보를 보여준다. 후보가 한 개이거나 입력에 대상 ID·36kg이 있어도 확인자 또는 확인 시각이 없으면 `UNCONFIRMED`, 연결 대상은 null이고 건별/주간 수령·차이·차액에 반영하지 않는다. 다른 범위의 수령에는 후보를 노출하지 않는다.
- 명시적인 `target_delivery_line_id`가 있으면 해당 행과 제공된 명세 번호·날짜·품목·규격이 모순되지 않아야 한다. 번호가 있으면 번호를 우선하고, 번호가 틀렸을 때 날짜로 우회 연결하지 않는다.
- 번호도 대상 행 ID도 없으면 납품일·품목·규격·단위가 모두 있어야 자동 연결한다. 정보가 비었거나 후보가 여러 개면 `receipt_links[].candidates`에 원본 근거를 포함한 후보와 보류 사유를 반환한다. 문자열은 정확히 비교하며 임의 유사도 연결은 하지 않는다.
- `receipt_id` 재전송과 같은 `source_line_id`의 반복 확인은 한 번만 집계한다. 같은 ID/원본 행이 서로 다른 값·대상으로 들어오면 보류한다. 직접 입력처럼 원본 행 ID가 없는 확인 이벤트는 서버가 부여한 동일 `receipt_id`로 재전송해야 한다. 서로 다른 수령 기록의 실제 중복 여부는 서버의 안정적인 ID와 확인 이력에 의존한다.
- 정정은 확인된 새 이벤트의 `supersedes_receipt_id`가 이전 이벤트를 가리킨다. 같은 대상의 전체 정정 이력을 입력하면 최신 이벤트만 집계한다. 선행 기록 누락·분기·순환·대상 변경은 보류한다. 미확인 정정은 기존 확인값을 대체하지 않는다.
- 단위 환산은 추측하지 않는다. 단위가 다른 수령은 원문을 `receipt_links[].receipt`에 보존하고 차이 계산을 보류한다. 확인된 환산이 필요하면 서버 어댑터가 수량·단위를 변환하고 원문·환산 근거를 보존한 뒤 호출해야 한다.
- 확인된 행은 계산값과 원본 근거를 유지한다. 보류 수령 또는 미확인 납품이 남으면 주간 수령·차이·차액은 null이다. A2와 동일하게 승인 대상액·저장 승인액은 null, 승인은 차단한다.

반환은 A2 계산 필드에 `receipt_links`, `blockers`, 점포·거래처·기간을 추가한 계산 결과 조각이다. 전체 API/DB 계약과 실제 자료 연동은 미검증이다.

검산: `python -m unittest app.reconcile.test_reconcile app.reconcile.test_matching -v`

## B2 · 주간/개별 검산

기존 `reconcile_receipts` → `reconcile_lines` 호출로 `summary`와 `items`를 분리한다. 별도 계산 함수나 숫자별 분기를 추가하지 않았다.

- 공개 합성 원본 A01~A04를 수동 전사한 로컬 입력: 주간 명세/청구/수령 100/100/96kg, 차이 4kg, 확인 차액 80,000원, 기재 청구액 2,000,000원.
- 개별 DL-A091은 60/60/60kg·차이 0kg·0원, DL-A131은 40/40/36kg·차이 4kg·80,000원.
- 추가 예시로 음수 소수 차이, 기재 청구액과 수량×단가의 불일치, 기재액 누락, 단가 누락, 명세/파일명 변경, 한 행만 수정, 부분 수령을 검산했다.
- 음수 차이와 차액은 부호를 보존한다. 기재 청구액을 재계산하지 않으며 누락이면 null이다. 단가가 없으면 수량 차이는 유지하고 차액만 null로 둔다.
- 테스트의 원본 ID·수령 확인자는 로컬 예시다. 실제 판독·점주 확인·서버 연동은 검증하지 않았다. 승인은 계속 차단한다.

전체 검산: `python -m unittest app.reconcile.test_reconcile app.reconcile.test_matching app.reconcile.test_weekly -v`

## C1~D2 · 기간 정산과 승인 직전 재계산

진입점은 `app.reconcile.settlement.reconcile_period`다. 기존 A2/B1 함수는 계산 조각으로 항상 승인을 차단하며, 전체 근거를 확인하는 이 진입점만 승인 가능 여부를 반환한다.

필수 키워드 인자는 `deliveries`, `invoices`, `receipts`, `memos`, `store_id`, `supplier_id`, `period_id`, `period_start`, `period_end`, `input_revision`이다. 선택 인자 `approvals`는 서버에 이미 저장된 승인본 메타데이터다. 최소 호출 예시는 `test_settlement.period_input()`의 공개 합성 자료 fixture를 참고한다. 제품에서 이 fixture를 불러오면 안 된다.

- `deliveries`: B1 행의 ID·거래 ID·명세 번호·납품일·품목·단위·`declared_qty`·명세 원본 근거. 원본별 알려진 미해결 사항은 `blockers`로 전달한다.
- `invoices`: 원본 `document_id`, `doc_no`, `doc_type`(INVOICE/CORRECTED_INVOICE), 점포·거래처·기간 ID, 명시된 `period_start/end`, `evidence_refs`, `lines`. 행은 `target_delivery_line_id`, `billed_qty`, `unit_price`, `unit`, `billed_amount`다. 청구 원본에서 납품 행으로 연결·정규화하는 어댑터는 서버가 제공한다. 발행일로 정산 기간을 추측하지 않는다.
- 수정 청구는 `replaces_doc_no` 또는 확인된 `replacement.target_document_id`로 기존 청구를 가리킨다. `replacement`에는 `confirmed_by/at`와 `evidence_refs`가 필요하다. 대상 불명확·동일 번호 다른 문서·복수 수정 후보는 합산하지 않고 보류한다.
- 대체 확인과 수량 합의는 별개다. `agreement`의 `supplier_confirmed=True`, `owner_confirmed=True`, `confirmed_by/at`, `evidence_refs`가 있어야 원래 명세와 수정 청구 차이의 차단을 해제한다. 문서 머리글 근거는 `line_id=None`을 허용한다. 이전 청구와 차이는 `invoice_history`에 보존한다.
- `memos`: 서버가 확인한 `memo_id`, 거래 ID, 범위 ID, `confirmed_text`, `source_mode`, `confirmed_by/at`. 수령값을 덮어쓰지 않는다. 구조화된 `supplier_claim_qty`·`unit` 또는 `conflicting=True`는 호출자가 확인한 주장 정보이며 이 함수가 자연어에서 추측하지 않는다. 충돌은 CONFLICTING/WAITING_REPLY와 승인 차단을 유지하고, 같은 행에 양측 합의된 수정 청구가 있으면 현재 충돌을 해소하되 메모는 보존한다.
- 승인 가능에는 모든 대상 행의 확인 수령·단위·금액·명세/수령/청구 근거와 빈 차단 사유가 필요하다. 부분 승인이나 단순히 주간 차이 합계가 0인 상황은 승인하지 않는다. 승인 전 `approved_amount=None`이다.
- `validate_approval(expected_revision=..., **period_input)`은 자료 버전 불일치 시 `ApprovalBlocked(code="STALE_REVISION", status_code=409)`, 미해결 상태이면 `BLOCKED/422`를 발생시킨다. 통과해도 승인본을 저장하지 않는다.
- **서버 연결 필수:** 구본혁이 기간 잠금 안에서 동일한 최신 DB 스냅샷을 읽어 이 함수를 호출하고, 권한·요청 키·동시 승인·버전을 검증한 뒤 스냅샷을 원자적으로 저장해야 한다. 같은 성공 요청 재시도의 기존 ID 반환도 서버 책임이다. 실제 API/DB 트랜잭션 연결은 미검증이다.
- `approvals` 중 현재 자료 버전의 승인본이 유효하면 APPROVED와 저장 승인액을 반환하고 추가 승인을 막는다. 새 버전은 승인액을 null로 두고 과거 승인본은 별도 보존한다. 송금 판단은 이 계산 범위에서 하지 않으며 `payment_status=UNCONFIRMED`를 유지한다. 실제 송금 증빙 처리는 서버에서 별도 연결한다.

전체 검산: `python -m unittest discover -s app/reconcile -t . -p "test_*.py" -v`

검증 범위: 공개 합성 원본을 수동 전사한 예시 및 회귀 테스트. 실제 사진 판독·점주 확인·API/DB 연동은 미실행이다.

## B1 · OCR 출력 연결부

GPT 전환 후 권장 진입점은 `app.reconcile.documents.reconcile_documents`입니다.
기존 `app.reconcile.ocr.reconcile_ocr_documents`는 호환 import로 유지하며 OCR 엔진을 호출하지 않습니다.
아래 OCR 설명은 이전 입력 계약 이력입니다. GPT 출력도 미확인 문서 사실로 취급하고 서버 확인 이벤트만 집계합니다.

`app.reconcile.ocr.reconcile_ocr_documents(documents, confirmations, *, store_id, supplier_id, period_id)`로 사진 판독 출력에서 B1 계산까지 연결한다. `app.photo.extract_document`의 문서/행 필드 구조를 사용하며, OCR을 다시 실행하거나 판독값을 추측하지 않는다.

호출 전 서버가 각 문서에 검증된 `store_id/supplier_id/period_id`와 각 행의 고유 `line_id`를 붙여야 한다. `document_id`도 필수다. 다른 원문 거래처·기간을 선택 중인 범위로 임의 지정하면 안 된다. 실제 서버 ID가 없는 로컬 검산에서는 명시적인 로컬 ID를 사용한다. 함수는 ID나 확인자를 자동 생성하지 않는다. `transaction_id`는 제공된 경우 보존하며 없으면 null이다.

- DELIVERY의 `qty_read` → `declared_qty`, INVOICE의 `qty_read/unit_price_read/amount_read` → `billed_qty/unit_price/billed_amount`. 수량과 단가는 원래 십진 문자열, 원화 기재액은 정수/null을 유지한다.
- 명세·청구는 B1과 같은 후보 검색 규칙을 공유한다. 모호한 행, 번호가 없는 불완전 후보, 단위 불명, 동일 청구 번호의 서로 다른 파일, 빈 판독 문서는 보류한다. 수정 청구는 합산하지 않고 `CORRECTION_REQUIRES_SETTLEMENT`로 반환한다. 유효 대체·양측 합의 처리는 C2 함수와 별도로 연결해야 한다.
- RECEIPT의 `qty_read`는 기본적으로 **미확인 후보**다. OCR 문서나 원본 행 안의 `confirmed_by/at`, `received_qty`, 연결 대상 필드는 확인 근거로 사용하지 않는다.
- `confirmations`에는 서버가 별도로 인증·저장한 수령 확인 이벤트만 전달한다. 동일 범위의 `source_line_id`로 판독 행을 찾는다. `receipt_id`, `confirmed_by/at`를 가진 이벤트가 있어야 집계한다. `received_qty`를 생략한 receipt_link는 해당 원본의 `qty_read`를 사용하며, 서버가 확인한 정정 수량은 이벤트의 값으로 명시한다. 같은 요청의 재전송은 같은 receipt_id를 사용한다.
- 확인 이벤트에 대응하는 원본 행이 없으면 `UNKNOWN_CONFIRMATION_SOURCE`로 보류한다. 원본 행 없는 직접 수령 입력(receipt_entry)은 이 OCR 전용 연결부가 아닌 B1 수령 함수에 전달한다.
- 원래 판독 행은 `ocr_line`, 기재 청구 행은 `invoice_ocr_line`, 문서·행·페이지/영역은 `evidence_refs`에 보존한다. 품목의 OCR 공백 차이는 연결용 값에서만 정규화하고 원문은 유지한다.
- 결과는 기존 B1 필드에 청구 행 연결 후보·사유인 `document_links`와 `excluded_document_ids`를 추가한다. 이 진입점은 승인하지 않는다. 실제 사진 판독 JSON과 API/DB 호출에 대한 검증은 별도다.

재현용 OCR 형식 예시는 `test_ocr.ocr_documents()`에 있다. 이는 엔진 실행 결과가 아닌 테스트 fixture다. 실제 판독 JSON을 받으면 서버 범위·ID를 명시해 같은 진입점으로 재검산한다.
