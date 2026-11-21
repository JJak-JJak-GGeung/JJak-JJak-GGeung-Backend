"""Reproduce B2 at the original A04 invoice baseline."""
import json
from pathlib import Path

from app.reconcile.matching import reconcile_receipts

path = Path(__file__).with_name("DL-A091_DL-A131_B2_대조입력.json")
inputs = json.loads(path.read_text(encoding="utf-8"))
result = reconcile_receipts(**inputs)
fields = ("declared_qty", "billed_qty", "received_qty", "delta_qty", "check_amount", "billed_amount")
assert tuple(result["summary"][field] for field in fields) == ("100", "100", "96", "4", 80000, 2000000)
expected = {"DL-A091": ("60", "60", "60", "0", 0, 1200000), "DL-A131": ("40", "40", "36", "4", 80000, 800000)}
assert {item["delivery_no"]: tuple(item[field] for field in fields) for item in result["items"]} == expected
assert all(link["status"] == "LINKED" for link in result["receipt_links"])
assert result["match_result"] == "MISMATCH" and not result["can_approve"]
print(json.dumps({"summary": result["summary"], "items": {item["delivery_no"]: {field: item[field] for field in fields} for item in result["items"]}, "can_approve": result["can_approve"]}, ensure_ascii=False, indent=2))
