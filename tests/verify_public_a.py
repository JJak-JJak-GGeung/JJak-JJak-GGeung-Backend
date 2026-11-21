"""Exercise public A against the deployed HTTP API on a fresh QA period."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--period", required=True)
parser.add_argument("--originals", type=Path, required=True)
parser.add_argument("--approval-id")
parser.add_argument("--base", default="https://jjak-jjak-ggeung-a1.rkrbhm0ryexya.ap-northeast-2.cs.amazonlightsail.com")
args = parser.parse_args()
origin = "https://jjak-jjak-ggeung-frontend.vercel.app"
period_path = "/api/periods/" + args.period


def call(path, body=None):
    request = Request(args.base + path, data=None if body is None else json.dumps(body).encode(),
                      headers={"Origin": origin, "Content-Type": "application/json"})
    with urlopen(request, timeout=180) as response:
        assert response.headers.get("Access-Control-Allow-Origin") == origin
        return json.load(response)


def expected_error(status, action):
    try:
        action()
    except HTTPError as error:
        assert error.code == status, error.read().decode()
        return
    raise AssertionError("Expected HTTP " + str(status))


def verify_snapshot(approval_id):
    fresh = build_opener()
    with fresh.open(args.base + period_path + "/approvals", timeout=30) as response:
        history = json.load(response)["approvals"]
    assert len(history) == 1 and history[0]["approval_id"] == approval_id
    with fresh.open(args.base + "/api/approvals/" + approval_id, timeout=30) as response:
        approved = json.load(response)
    assert approved["approved_amount"] == 1920000 and approved["payment_status"] == "UNCONFIRMED"
    assert approved["snapshot"]["items"]
    for source in approved["snapshot"]["documents"]:
        if source["source_mode"] != "FILE":
            continue
        with fresh.open(args.base + "/api/documents/" + source["id"] + "/original", timeout=30) as response:
            data = response.read()
        assert hashlib.sha256(data).hexdigest() == source["file_hash"]
        assert data == (args.originals / source["filename"]).read_bytes()
    assert len(call(period_path + "/approvals")["approvals"]) == 1
    return approved


if args.approval_id:
    approved = verify_snapshot(args.approval_id)
    current = call(period_path)
    if current["input_revision"] == approved["input_revision"]:
        assert current["approved_amount"] == approved["approved_amount"]
    else:
        assert current["input_revision"] > approved["input_revision"]
        assert current["approved_amount"] is None
        print("PASS later evidence starts a new review while the original approval snapshot is preserved")
    print("PASS new-session approval list/detail, version-aware period state, original bytes and unchanged approval count")
    raise SystemExit(0)

state = call(period_path)
for document in state["documents"]:
    if document["source_mode"] == "FILE":
        value = call("/api/documents/" + document["document_id"] + "/extraction", {})
        assert value["extraction_status"] == "COMPLETED", value
        print("Extracted", value["filename"], len(value["lines"]), flush=True)
state = call(period_path)
assert len(state["items"]) == 2, state
receipt_doc = next(d for d in state["documents"] if d["doc_type"] == "RECEIPT")
for line in receipt_doc["lines"]:
    item = next(item for item in state["items"] if item["delivery_no"] == line["delivery_no"])
    delivery_id = next(ref["line_id"] for ref in item["evidence_refs"] if ref["kind"] == "DELIVERY")
    body = {"kind": "receipt_link", "source_line_id": line["line_id"], "target_id": delivery_id,
            "evidence_refs": [{"document_id": receipt_doc["document_id"], "line_id": line["line_id"],
                               "source_location": line["source_location"]}],
            "expected_revision": state["input_revision"], "request_key": "public-a-receipt-" + line["line_id"]}
    saved = call(period_path + "/confirmations", body)
    retry = call(period_path + "/confirmations", body)
    assert retry["confirmation_id"] == saved["confirmation_id"]
    state = call(period_path)
assert state["summary"]["billed_qty"] == "100"
assert state["summary"]["received_qty"] == "96"
assert state["summary"]["check_amount"] == 80000
assert state["summary"]["billed_amount"] == 2000000
assert state["approved_amount"] is None and not state["can_approve"]
print("PASS confirmed receipts: 100/100/96 kg, 80000 won difference", flush=True)

item = next(item for item in state["items"] if item["check_amount"] == 80000)
memo = {"confirmed_text": "거래처는 11월 13일에 장어 40킬로를 다 보냈다고 합니다. 우리 가게 기록에는 36킬로입니다.",
        "source_mode": "TYPED_TEXT", "expected_revision": state["input_revision"],
        "request_key": "public-a-memo-" + item["transaction_id"]}
memo_path = "/api/transactions/" + item["transaction_id"] + "/memos"
saved_memo = call(memo_path, memo)
assert call(memo_path, memo)["memo_id"] == saved_memo["memo_id"]
detail = call("/api/transactions/" + item["transaction_id"])
assert detail["memos"][0]["confirmed_text"] == memo["confirmed_text"]
state = call(period_path)
assert state["fact_status"] == "CONFLICTING"
expected_error(422, lambda: call(period_path + "/approvals", {
    "expected_revision": state["input_revision"], "request_key": "public-a-blocked"}))
print("PASS memo save/retry/reload and unresolved approval rejection", flush=True)

corrected = next(d for d in state["documents"] if d["doc_type"] == "CORRECTED_INVOICE")
original = next(d for d in state["documents"] if d["doc_no"] == corrected["replaces_doc_no"])
replacement = {"kind": "replacement", "document_id": corrected["document_id"],
               "target_id": original["document_id"],
               "evidence_refs": [{"document_id": corrected["document_id"], "line_id": None,
                                  "source_location": corrected["agreement_source_location"]}],
               "expected_revision": state["input_revision"], "request_key": "public-a-replacement"}
saved = call(period_path + "/confirmations", replacement)
assert call(period_path + "/confirmations", replacement)["confirmation_id"] == saved["confirmation_id"]
state = call(period_path)
assert state["can_approve"], state["blockers"]
assert state["summary"]["billed_qty"] == state["summary"]["received_qty"] == "96"
assert state["approvable_amount"] == 1920000 and state["approved_amount"] is None
print("PASS corrected invoice: 1920000 won approvable, approved amount null", flush=True)

revision = state["input_revision"]
bodies = [{"expected_revision": revision, "request_key": "public-a-approve-" + str(i)} for i in range(2)]
with ThreadPoolExecutor(max_workers=2) as pool:
    approvals = list(pool.map(lambda body: call(period_path + "/approvals", body), bodies))
assert approvals[0]["approval_id"] == approvals[1]["approval_id"]
approval = approvals[0]
assert call(period_path + "/approvals", bodies[0]) == approval
expected_error(409, lambda: call(period_path + "/approvals", {
    "expected_revision": revision - 1, "request_key": "public-a-stale"}))
verify_snapshot(approval["approval_id"])
print("PASS concurrent approval, old request replay, new-session list/detail and all original bytes", flush=True)
print(json.dumps(approval, ensure_ascii=False), flush=True)
