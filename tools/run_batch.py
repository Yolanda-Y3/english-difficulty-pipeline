"""Independent per-document runs, optional budget, and durable results."""
import argparse
import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from english_difficulty.resources import Bundle
from english_difficulty.provider import ArkMini, ProviderError
from english_difficulty.pipeline import Pipeline

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=None, help='Optional user-requested batch budget; default has no batch call cap')
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--only', nargs='*', help='Explicit document IDs for targeted recovery')
    parser.add_argument('--fresh-documents', nargs='*', default=[], help='Do not reuse any checkpoint for these document IDs')
    parser.add_argument('--retry-missing', action='store_true', help='Also retry null-score dimension judgments; keep successful scored dimensions')
    parser.add_argument('--repair-guidance', type=Path, help='External format/contract repair instructions; never replace rubric rules')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    repair_guidance = args.repair_guidance.read_text(encoding='utf-8') if args.repair_guidance else None
    if repair_guidance:
        (args.output/'repair_guidance.md').write_text(repair_guidance,encoding='utf-8')
    documents = json.loads(args.input.read_text(encoding='utf-8'))
    previous = {}
    if args.previous:
        previous = {d['id']:d for d in json.loads((args.previous/'result.json').read_text(encoding='utf-8'))['documents']}
        for key, old in previous.items():
            per_doc = args.previous/(key+'.json')
            if per_doc.exists():
                saved_run = json.loads(per_doc.read_text(encoding='utf-8'))
                old['saved_trace'] = saved_run.get('trace',[])
                old['_checkpoint_version'] = saved_run.get('versions',{}).get('pipeline')
        documents = [d for d in documents if d['id'] in previous and (args.only or not previous[d['id']].get('units') or any(
            u['status']=='unavailable' or any(x.get('score_basis',{}).get('score_alignment')=='unavailable' or (args.retry_missing and x.get('score') is None) for x in u.get('dimensions',[]))
            for u in previous[d['id']]['units']))]
        documents.sort(key=lambda d: -sum(len(u.get('completed_stages',{})) for u in previous[d['id']].get('units',[])))
    if args.only:
        documents = [d for d in documents if d['id'] in args.only]
    bundle = Bundle(ROOT)
    snapshot = {'config':bundle.config,'prompts':bundle.prompts,'schemas':bundle.schemas,
                'rubrics':bundle.rubrics,'resources':bundle.resources,'routes':bundle.routes}
    (args.output/'configuration_snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    lock = threading.Lock()
    used = 0
    class BudgetProvider(ArkMini):
        def __init__(self, config, old):
            super().__init__(config)
            self.old = old
        def complete(self, system, payload, schema, node):
            nonlocal used
            if node == 'structure' and self.old.get('structure'):
                return copy.deepcopy(self.old['structure'])
            u = next((u for u in self.old.get('units',[]) if u['unit_id']==payload.get('unit',{}).get('id')), {})
            routing = u.get('routing', u.get('completed_stages',{}).get('routing'))
            if node == 'route' and routing:
                return copy.deepcopy(routing)
            # Unified nodes may only reuse successful outputs from this version.
            if node == 'dimension' and self.old.get('_checkpoint_version') == bundle.config['version']:
                dim = payload['rubric']['dimensions'][0]['id']
                saved = [t['output'] for t in self.old.get('saved_trace', [])
                         if t['stage'] == node and t.get('dimension_id') == dim
                         and t['outcome'] == 'ok' and t.get('unit_id') == payload['unit']['id']]
                if saved and not (args.retry_missing and saved[-1]['dimensions'][0]['score'] is None):
                    return copy.deepcopy(saved[-1])
                if not payload.get('repair'):
                    prior = [t for t in self.old.get('saved_trace', [])
                             if t['stage'] == node and t.get('dimension_id') == dim
                             and t.get('unit_id') == payload['unit']['id']]
                    if prior:
                        last = prior[-1]
                        payload = {**payload, 'repair': {
                            'validation_error': last.get('reason') or 'Previous dimension returned no score. Recheck whether the supplied full question and rubric suffice; complete your own analysis. Keep null only for an actual missing source or unresolved rule boundary.',
                            'previous_output': last.get('output')}}
            if node == 'dimension' and repair_guidance:
                payload = {**payload, 'repair': {**(payload.get('repair') or {}), 'contract_guidance': repair_guidance}}
            with lock:
                if args.max_calls is not None and used >= args.max_calls:
                    raise ProviderError('shared request budget exhausted', False)
                used += 1
            return super().complete(system,payload,schema,node)
    def work(doc):
        provider = BudgetProvider(bundle.config['provider'], {} if doc['id'] in args.fresh_documents else previous.get(doc['id'],{}))
        result = Pipeline(bundle,provider).run([doc])
        (args.output/(doc['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(work,d) for d in documents]):
            result = future.result()
            results.append(result)
            doc = result['documents'][0]
            print(json.dumps({'id':doc['id'],'units':[(u['status'],u.get('score'),u.get('reason')) for u in doc['units']], 'calls':used},ensure_ascii=False),flush=True)
    by_id = {r['documents'][0]['id']:r['documents'][0] for r in results}
    final = {'versions':bundle.snapshot(),'documents':[by_id[d['id']] for d in documents],
             'summary':{'network_calls':sum(r['summary']['network_calls'] for r in results),'budget':args.max_calls},
             'usage':[u for r in results for u in r['usage']]}
    (args.output/'result.json').write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(final['summary']),flush=True)

if __name__ == '__main__': main()
