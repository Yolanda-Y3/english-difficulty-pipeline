import json
from pathlib import Path
from .contracts import require

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

class Bundle:
    def __init__(self, root):
        self.root = Path(root)
        self.config = read_json(self.root / 'config/pipeline.json')
        provider = self.config['provider']
        require(provider['model'] == 'doubao-seed-2-0-mini-260215', 'only Doubao 2.0 mini is permitted')
        require(provider['allowed_models'] == [provider['model']], 'model fallback is forbidden')
        require(provider['base_url'] == 'https://ark.cn-beijing.volces.com/api/v3', 'unexpected provider endpoint')
        require(provider.get('response_format') == 'json_schema' and provider.get('strict_schema') is True,
                'strict server-side schema is required')
        self.routes = read_json(self.root / 'config/routes.json')['routes']
        self.rubrics = {r['id']: read_json(self.root / 'config' / r['rubric']) for r in self.routes}
        self.schemas = {n: read_json(self.root / 'schemas' / p) for n, p in self.config['schemas'].items()}
        self.prompts = {n: (self.root / 'prompts' / p).read_text(encoding='utf-8') for n, p in self.config['prompts'].items()}
        self.resources = {n: read_json(self.root / 'resources' / p) for n, p in self.config['resources'].items()}
        self.vocabulary = {}
        for entry in self.resources['vocabulary']['entries']:
            self.vocabulary.setdefault(entry['lemma'], []).append(entry)
        require(len(self.routes) == 11 and sum(len(r['dimensions']) for r in self.rubrics.values()) == 58, 'route/dimension coverage mismatch')
        for rubric in self.rubrics.values():
            require(abs(sum(d['weight'] for d in rubric['dimensions']) - 1) < 1e-9, 'invalid weights')
        require(1 <= self.config['limits']['attempts_per_node'] <= 3, 'unbounded retry configuration')
        require(0 < self.config['limits']['max_calls_per_run'] <= 1000, 'unbounded run budget')
        require(self.config['policy']['manual_wait'] is False, 'manual waits forbidden')

    def lookup(self, lexical):
        result = []
        dictionary = self.resources['dictionary']
        configured = bool(dictionary['edition'] and dictionary['rank_scope'] in ('lemma', 'pos'))
        for word in lexical['occurrences']:
            entries = self.vocabulary.get(word['lemma'].lower(), [])
            frequencies = {e['frequency'] for e in entries}
            def frequency_class(f):
                cfg = self.config['lexical']
                return 'high' if f > cfg['high_above'] else 'low' if f < cfg['low_below'] else 'middle'
            classes = {frequency_class(f) for f in frequencies}
            candidates = [e for e in dictionary['entries'] if e['lemma'].lower() == word['lemma'].lower() and e['pos'] == word['pos']] if configured else []
            result.append({**word, 'in_vocabulary': bool(entries), 'frequency_class': next(iter(classes)) if len(classes) == 1 else 'unknown',
                           'vocabulary_sources': entries, 'dictionary_candidates': candidates,
                           'dictionary_version': dictionary['version'], 'rare_sense': 'unknown',
                           'oov': False if entries or word['exemption'] else 'candidate_requires_context_evidence'})
        return {'complete': lexical['complete'], 'coverage_note': lexical['coverage_note'], 'occurrences': result}

    def senses(self, lookup, matches):
        by_id = {x['id']: x for x in lookup['occurrences']}
        result = []
        seen = set()
        for match in matches:
            require(match['occurrence_id'] in by_id and match['occurrence_id'] not in seen, 'invalid/duplicate sense occurrence')
            seen.add(match['occurrence_id'])
            word = by_id[match['occurrence_id']]
            candidates = {e['sense_id']: e for e in word['dictionary_candidates']}
            sid = match['sense_id']
            require(sid is None or sid in candidates, 'fabricated dictionary sense')
            require(match['ref'] == word['ref'], 'sense reference differs from lexical occurrence')
            entry = candidates.get(sid)
            example_lemma = match.get('example_lemma')
            examples = {e['lemma']:e for e in self.resources['rare_examples']['entries']}
            actual_example = examples.get(word['lemma'].lower())
            source_warnings = []
            if example_lemma is not None and (example_lemma not in examples or example_lemma != word['lemma'].lower()):
                source_warnings.append('模型声称的样例来源不存在或不匹配；按语境模型判断保留，未认定为样例佐证')
            basis = match.get('basis', 'dictionary')
            if basis == 'contextual_model':
                require(self.config['lexical']['rare_sense_method'] == 'contextual_model_with_examples', 'contextual judgment disabled')
                require(sid is None, 'contextual judgment cannot claim dictionary sense id')
                require(match.get('judgment') in ('rare','common','uncertain'), 'missing contextual judgment')
                require(all(type(match.get(k)) is str and match[k].strip() for k in ('common_sense','contextual_sense','reason')), 'contextual comparison required')
                rare = 'unknown' if match['judgment'] == 'uncertain' else bool(word['in_vocabulary'] and match['judgment'] == 'rare')
            else:
                require(basis == 'dictionary' and entry is not None, 'dictionary judgment requires actual entry')
                rare = bool(word['in_vocabulary'] and entry['rank'] >= self.config['lexical']['rare_min_rank'])
            result.append({**match, 'rare_sense': rare, 'dictionary_entry': entry,
                           'example_lemma': actual_example['lemma'] if actual_example else None,
                           'example_source': actual_example, 'source_warnings': source_warnings,
                           'model_reason': match['reason'],
                           'reason': f"常见义：{match.get('common_sense', '')}；当前义：{match.get('contextual_sense', '')}；结论：{match.get('judgment', '')}。" if source_warnings else match['reason'],
                           'rule_basis': 'contextual_comparison_not_dictionary_rank' if basis == 'contextual_model' else 'verified_dictionary_rank'})
        return result

    def snapshot(self):
        return {'pipeline': self.config['version'], 'model': self.config['provider']['model'],
                'rubrics': {k: v['version'] for k, v in self.rubrics.items()},
                'resources': {k: v['version'] for k, v in self.resources.items()}}
