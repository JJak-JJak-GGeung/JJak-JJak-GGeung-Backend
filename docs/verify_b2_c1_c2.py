"""Offline source replay; never calls a server or creates owner confirmations."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--originals', type=Path, default=ROOT/'team-work-pack',
                    help='Directory containing the public A05 PNG and A06 text (not private QA data).')
args = parser.parse_args()
sys.path.insert(0, str(ROOT.parent))
from app.reconcile.matching import reconcile_receipts
from app.reconcile.settlement import reconcile_period

OUT = ROOT / 'integration' / 'reconcile-handoff'
OUT.mkdir(exist_ok=True)
scope = dict(store_id='offline-store', supplier_id='offline-supplier', period_id='offline-period')
paths = [ROOT/'integration/b1/DL-A091_B2_실제판독.json', ROOT/'integration/b1/DL-A131_B1_실제판독.json']
bundles = [json.loads(p.read_text(encoding='utf-8')) for p in paths]
lines, receipts = [], []
for b in bundles:
    docs = {d['doc_type']: d for d in b['documents']}
    delivery, invoice, receipt = (docs[k] for k in ('DELIVERY', 'INVOICE', 'RECEIPT'))
    assert all(d['delivery_no'] == b['matching_context']['delivery_no'] for d in docs.values())
    assert all(d['unit'] == 'kg' for d in docs.values())
    assert all(d['store_name'] == b['matching_context']['store_name'] for d in docs.values())
    assert all(d['supplier_name_read'] == b['matching_context']['supplier_name'] for d in docs.values())
    # Offline aliases do not assert that the two source bundles share verified server scope.
    def evidence(d):
        return {'document_id': d['document_id'] or 'offline:'+d['fixture_ref'],
                'line_id': d['line_id'], 'kind': d['doc_type'], 'source_location': d['source_location']}
    no = delivery['delivery_no']
    line = {**scope, 'line_id':'offline:'+no, 'transaction_id':'offline:transaction:'+no,
            **{k:delivery[k] for k in ('delivery_no','delivery_date','item','spec','unit')},
            'declared_qty':delivery['qty_read'], 'billed_qty':invoice['qty_read'],
            'unit_price':invoice['unit_price_read'], 'billed_amount':invoice['amount_read'],
            'evidence_refs':[evidence(delivery), evidence(invoice)]}
    event = {**scope, **deepcopy(b['receipt']), 'delivery_no':no,
             'unit':receipt['unit'], 'qty_read':receipt['qty_read'],
             'evidence_refs':[evidence(receipt)]}
    event['target_delivery_line_id'] = line['line_id'] if event.get('target_delivery_line_id') else None
    lines.append(line);receipts.append(event)

cases = {}
def record(name, inputs, output, mode):
    cases[name] = {'mode':mode, 'input':deepcopy(inputs), 'output':output}

raw = reconcile_receipts(lines, receipts, **scope)
assert raw['summary']['declared_qty'] == raw['summary']['billed_qty'] == '100'
assert all(raw['summary'][k] is None for k in ('received_qty','delta_qty','check_amount'))
assert raw['items'][0]['received_qty'] == '60' and raw['items'][0]['check_amount'] == 0
assert raw['items'][1]['received_qty'] is None and not raw['can_approve']
record('B2_received_sources', dict(lines=lines,receipts=receipts,**scope), raw,
       'Actual supplied OCR values; A091 reported confirmation retained; A131 remains unconfirmed. Offline scope aliases.')
confirmed = deepcopy(receipts)
confirmed[1].update(receipt_id='simulated:A131', source_line_id='simulated:A03:2',
                    target_delivery_line_id=lines[1]['line_id'], received_qty='36',
                    confirmed_by='SIMULATED_OWNER', confirmed_at='2026-11-21T00:00:00+00:00')
both = reconcile_receipts(lines, confirmed, **scope)
assert (both['summary']['received_qty'],both['summary']['delta_qty'],both['summary']['check_amount']) == ('96','4',80000)
assert both['summary']['billed_amount'] == 2000000 and not both['can_approve']
record('B2_simulated_both_confirmed', dict(lines=lines,receipts=confirmed,**scope),both,
       'Actual OCR quantities plus explicitly simulated A131 owner confirmation; not server proof.')

invoice = {**scope, 'document_id':'offline:A04','doc_no':'INV-A01','doc_type':'INVOICE',
           'period_start':'2026-11-09','period_end':'2026-11-15',
           'evidence_refs':[{'document_id':'offline:A04','line_id':None,'kind':'INVOICE',
                             'source_location':{'page':1,'text':'invoice header'}}],
           'lines':[{'target_delivery_line_id':l['line_id'], **{k:l[k] for k in ('billed_qty','unit_price','unit','billed_amount')}} for l in lines]}
base = {**scope, 'period_start':'2026-11-09','period_end':'2026-11-15','input_revision':1,
        'deliveries':[{**l,'evidence_refs':[l['evidence_refs'][0]]} for l in lines],
        'invoices':[invoice], 'receipts':confirmed, 'memos':[]}
text_path = next(args.originals.glob('A06_*.txt'))
memo_text = text_path.read_text(encoding='utf-8').strip()
memo = {**scope,'memo_id':'simulated:memo:A06','transaction_id':lines[1]['transaction_id'],
        'confirmed_text':memo_text,'source_mode':'TYPED_TEXT','supplier_claim_qty':'40','unit':'kg',
        'confirmed_by':'SIMULATED_OWNER','confirmed_at':'2026-11-21T00:00:00+00:00'}
c1 = deepcopy(base);c1['memos']=[memo,memo]
result = reconcile_period(**c1)
assert result['fact_status']=='CONFLICTING' and result['work_status']=='WAITING_REPLY'
assert (result['summary']['received_qty'], result['summary']['check_amount'], result['summary']['billed_amount']) == ('96',80000,2000000)
assert len(result['memos'])==1 and not result['can_approve']
record('C1_A06_text_simulated_confirmation',c1,result,
       'Provided A06 text, manually structured claim 40kg, simulated confirmation. No audio transcription/NLP/API/DB verified.')

# A05 visually transcribed from the supplied public synthetic PNG; not an OCR engine result.
correction = deepcopy(invoice)
correction.update(document_id='offline:A05',doc_no='INV-A01-R1',doc_type='CORRECTED_INVOICE',
                  doc_date='2026-11-17',replaces_doc_no='INV-A01')
correction['lines'][1].update(billed_qty='36',billed_amount=720000)
correction['evidence_refs']=[{'document_id':'offline:A05','line_id':None,'kind':'INVOICE',
    'source_location':{'page':1,'text':'11월 13일 납품분을 양측 확인 후 36kg으로 수정했습니다.'}}]
confirmation={'confirmed_by':'SIMULATED_OWNER','confirmed_at':'2026-11-21T00:00:00+00:00',
              'evidence_refs':deepcopy(correction['evidence_refs'])}
for label, replacement, agreement in [('unconfirmed',False,False),('replacement_only',True,False),('bilateral_agreement',True,True)]:
    inp=deepcopy(c1);doc=deepcopy(correction)
    if replacement: doc['replacement']={**confirmation,'target_document_id':'offline:A04'}
    if agreement: doc['agreement']={**confirmation,'supplier_confirmed':True,'owner_confirmed':True}
    inp['invoices'].append(doc)
    output=reconcile_period(**inp)
    assert output['can_approve'] is agreement
    assert output['approved_amount'] is None
    if agreement:
        assert output['approvable_amount']==1920000 and output['summary']['check_amount']==0
        assert output['invoice_history'][0]['summary']['check_amount']==80000
    else: assert output['approvable_amount'] is None and output['blockers']
    record('C2_A05_'+label,inp,output,'A05 manually transcribed; replacement/agreement events simulated, no actual owner confirmation.')

sources=paths+[text_path,next(args.originals.glob('A05_*.png'))]
report={'created_at':datetime.now(timezone.utc).isoformat(),
        'code_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT.parent,text=True).strip(),
        'A091_source_commit':'209e33a4448688691c37ec13857784c3e4fe439e',
        'network_or_db_called':False,'cases':cases,
        'sources':[{'path':str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else p.name,
                    'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources],
        'not_verified':['live API/DB/authentication','A131 real owner confirmation and server row mapping',
                        'A06 actual audio transcription and memo persistence','A05 OCR extraction and actual replacement/agreement events',
                        'atomic approval persistence/retries/concurrency','deployment/cross-browser approval lookup']}
(OUT/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
for name,c in cases.items(): print(name,json.dumps(c['output']['summary']), 'can_approve='+str(c['output']['can_approve']))
print('PASS: 6 source replay cases; results.json includes all inputs, outputs and limitations.')
