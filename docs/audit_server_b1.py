"""Offline audit of actual server calculate() source; never imports API or calls DB."""
import ast
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
from uuid import UUID, uuid5

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from app.reconcile.settlement import reconcile_period
from app.reconcile.matching import delivery_candidates

SOURCE = ROOT / "server-audit/settlement_5abcde45.py"
source = SOURCE.read_text(encoding="utf-8")

old = '''    def target(line):
        candidates = [row for row in deliveries
                      if row["delivery_no"] == line["delivery_no"] and row["delivery_no"]
                      and row["item"] == line["raw_name"]
                      and row["unit"] == line["raw_unit"]]
        return candidates[0]["line_id"] if len(candidates) == 1 else None'''
new = '''    def target(line):
        fields = {"delivery_no": line["delivery_no"], "delivery_date": line["delivery_date"],
                  "item": line["raw_name"], "spec": line["raw_spec"], "unit": line["raw_unit"]}
        if not fields["delivery_no"] and not all(fields[key] for key in ("delivery_date", "item", "spec", "unit")):
            warnings.add("INCOMPLETE_BILLING_MATCH")
            return None
        candidates = delivery_candidates(deliveries, fields)
        if len(candidates) == 1 and fields["unit"] and fields["unit"] == candidates[0]["unit"]:
            return candidates[0]["line_id"]
        warnings.add("UNRESOLVED_BILLING_MATCH")
        return None'''
assert old in source
patched = source.replace(old, new, 1)

def load_calculate(text):
    tree = ast.parse(text)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ("quantity", "money", "evidence", "calculate")]
    assert len(functions) == 4
    namespace = {"deepcopy": deepcopy, "Decimal": Decimal, "UUID": UUID, "uuid5": uuid5,
                 "reconcile_period": reconcile_period, "delivery_candidates": delivery_candidates}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["calculate"]

def state(received="40"):
    period = {"id": str(UUID(int=1)), "store_id": str(UUID(int=2)), "supplier_id": str(UUID(int=3)),
              "period_start": "2026-11-09", "period_end": "2026-11-15", "input_revision": 8}
    documents, lines = [], []
    for kind, prefix in (("DELIVERY", "delivery"), ("INVOICE", "invoice"), ("RECEIPT", "receipt")):
        documents.append({"id": prefix, "doc_type": kind, "doc_no": prefix+"-number",
                          "supplier_id": period["supplier_id"], "source_mode": "FILE",
                          "extraction_status": "COMPLETED", "link_status": "LINKED",
                          "metadata": {"doc_type_read": kind},
                          "period_start": period["period_start"], "period_end": period["period_end"],
                          "replaces_doc_no": None, "agreement_text": None, "agreement_source_location": None})
        lines.append({"id": prefix+"-line", "document_id": prefix, "delivery_no": "delivery-number",
                      "delivery_date": "2026-11-13", "raw_name": "eel", "raw_spec": "trimmed",
                      "raw_unit": "kg", "qty_read": received if kind == "RECEIPT" else "40",
                      "unit_price_read": "20000", "amount_read": 800000,
                      "source_location": {"page": 1, "row": 1}})
    receipts = [{"id": "confirmed-receipt", "source_line_id": "receipt-line", "doc_line_id": "delivery-line",
                 "received_qty": received, "unit": "kg", "confirmed_by": "local-owner", "actor_label": "local-owner",
                 "confirmed_at": "2026-11-21T11:00:00+09:00", "supersedes_id": None}]
    return {"period": period, "documents": documents, "lines": lines, "receipts": receipts,
            "confirmations": [], "memos": [], "approvals": []}

cases = {"confirmed_mismatch": state("36"), "matched": state(), "no_number": state(),
         "different_spec": state(), "different_delivery_date": state(), "incomplete_fallback": state()}
for line in cases["no_number"]["lines"]:
    line["delivery_no"] = None
cases["different_spec"]["lines"][1]["raw_spec"] = "other-grade"
cases["different_delivery_date"]["lines"][1]["delivery_date"] = "2026-11-14"
for line in cases["incomplete_fallback"]["lines"]:
    line.update(delivery_no=None, raw_spec=None)
original, proposed = load_calculate(source), load_calculate(patched)
results = {}
for name, inputs in cases.items():
    before, _ = original(deepcopy(inputs))
    after, _ = proposed(deepcopy(inputs))
    results[name] = {"input": inputs, "current_server": before, "proposed_patch": after}
    print(name, json.dumps({"current_can_approve": before["can_approve"], "proposed_can_approve": after["can_approve"],
                            "current_blockers": before["blockers"], "proposed_blockers": after["blockers"]}))
assert not results["confirmed_mismatch"]["current_server"]["can_approve"]
assert results["confirmed_mismatch"]["current_server"]["summary"]["check_amount"] == 80000
assert results["matched"]["current_server"]["can_approve"] and results["matched"]["proposed_patch"]["can_approve"]
assert not results["no_number"]["current_server"]["can_approve"] and results["no_number"]["proposed_patch"]["can_approve"]
for name in ("different_spec", "different_delivery_date"):
    assert results[name]["current_server"]["can_approve"]
    assert not results[name]["proposed_patch"]["can_approve"]
assert not results["incomplete_fallback"]["proposed_patch"]["can_approve"]

import difflib
patch = ''.join(difflib.unified_diff(source.splitlines(keepends=True),
    patched.replace('from app.reconcile.settlement import reconcile_period',
                    'from app.reconcile.settlement import reconcile_period\nfrom app.reconcile.matching import delivery_candidates').splitlines(keepends=True),
    fromfile='a/app/workflow/settlement.py', tofile='b/app/workflow/settlement.py'))
(ROOT / "server-audit/B1_server_matching_proposal.patch").write_text(patch, encoding="utf-8")
(ROOT / "server-audit/B1_server_contract_audit.json").write_text(json.dumps({
    "server_source_commit": "5abcde45f3fa31a4b62e974a32f45ceccc63b936", "mode": "offline synthetic DB-state replay",
    "network_or_db_called": False, "server_files_modified": False, "patch_applied_to_server": False,
    "cases": results}, ensure_ascii=False, indent=2), encoding="utf-8")
print("6 offline cases verified; server-side proposal prepared but not applied")
