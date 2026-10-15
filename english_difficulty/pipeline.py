import copy
from .contracts import ContractError, require, validate, unique, check_refs, check_structure, check_dimensions, align_unique_quotes
from .provider import ProviderError
from .scoring import project_dimension, aggregate, token_selectors
from .model_output import normalize_dimension_container, project_counted_syntax, apply_existing_projection, align_original_anchor

class Pipeline:
    def __init__(self, bundle, provider):
        self.bundle, self.provider = bundle, provider
        self.attempts = 0
        self.trace = []
        self.provider_blocked = None

    def node(self, stage, payload, blocks, extra=None):
        cfg = self.bundle.config
        last = None
        previous = None
        schema = copy.deepcopy(self.bundle.schemas[stage])
        if stage == 'dimension' and payload.get('rubric'):
            dimensions=schema['properties']['dimensions']
            ids=[d['id'] for d in payload['rubric']['dimensions']]
            dimensions['minItems']=dimensions['maxItems']=len(ids)
            dimensions['items']['properties']['id']={'type':'string','enum':ids}
            dimensions['items']['properties']['facts']['items']['properties']['refs']['minItems'] = 1
        if stage == 'dimension' and payload.get('rubric', {}).get('route') == 'grammar_choice' and payload['rubric']['dimensions'][0]['id'] == 'd4':
            metrics = schema['properties']['dimensions']['items']['properties']['metrics']
            ref_array = self.bundle.schemas['route']['properties']['refs']
            metrics.update({'properties': {'option_evaluations': {'type':'array','items':{'type':'object','properties':{
                'is_correct':{'type':'boolean'},'temporarily_valid':{'type':'boolean'},
                'requires_rule_elimination':{'type':'boolean'},'reason':{'type':'string'},'refs':ref_array},
                'required':['is_correct','temporarily_valid','requires_rule_elimination','reason','refs'],
                'additionalProperties':False}},
                'distinction':{'enum':['same_grammar_forms','fine_rules',None]}},'required':['option_evaluations','distinction']})
        if stage == 'dimension' and 'judgment_metrics' in self.bundle.schemas:
            metrics = schema['properties']['dimensions']['items']['properties']['metrics']
            audit_schema = self.bundle.schemas['judgment_metrics']
            metrics.setdefault('properties', {}).update(copy.deepcopy(audit_schema['properties']))
            metrics.setdefault('required', []).extend(audit_schema['required'])
            if payload['rubric']['dimensions'][0]['id'] in cfg.get('counted_dimensions',{}).get(payload['rubric']['route'],[]):
                metrics['required'].extend(['judgment_items','judgment_count'])
            projection = payload['rubric']['dimensions'][0].get('projection')
            if projection == 'seven_length':
                metrics['properties']['passage_refs'] = copy.deepcopy(self.bundle.schemas['route']['properties']['refs'])
                metrics['required'].append('passage_refs')
            elif projection == 'cloze_span':
                metrics['properties']['clue_items'] = {'type':'array','items':{'type':'object','properties':{
                    'item_id':{'type':'string'},
                    'scope':{'enum':['same_sentence','adjacent_sentence','cross_paragraph','global']},
                    'refs':copy.deepcopy(self.bundle.schemas['route']['properties']['refs'])},
                    'required':['item_id','scope','refs'],'additionalProperties':False}}
                metrics['required'].append('clue_items')
            metrics['required'] = list(metrics['properties'])
            metrics['additionalProperties'] = False
        for attempt in range(cfg['limits']['attempts_per_node']):
            if self.provider_blocked:
                raise ProviderError(self.provider_blocked, False)
            if self.attempts >= cfg['limits']['max_calls_per_run']:
                raise ProviderError('整批模型请求次数达到上限', False)
            self.attempts += 1
            value = None
            entry = {'stage': stage, 'unit_id': payload.get('unit', {}).get('id'), 'attempt': attempt + 1}
            if stage == 'dimension':
                entry['dimension_id'] = payload['rubric']['dimensions'][0]['id']
                entry['node_id'] = payload['rubric']['route'] + '.' + entry['dimension_id']
            try:
                repair = {'validation_error': str(last), 'previous_output': previous} if last else None
                lengths = {key: len(text) for key, text in blocks.items()}
                prompt_key = cfg.get('dimension_prompt_overrides',{}).get(payload.get('rubric',{}).get('route'),stage) if stage=='dimension' else stage
                prompt = self.bundle.prompts['common'] + '\n' + self.bundle.prompts[prompt_key]
                if stage == 'dimension' and 'judgment' in self.bundle.prompts:
                    prompt += '\n' + self.bundle.prompts['judgment']
                    guidance = cfg.get('dimension_guidance', {}).get(payload['rubric']['route'], {}).get(payload['rubric']['dimensions'][0]['id'])
                    if guidance:
                        prompt += '\n' + self.bundle.prompts[guidance]
                    if repair and 'repair_contract' in self.bundle.prompts:
                        prompt += '\n' + self.bundle.prompts['repair_contract']
                value = self.provider.complete(prompt,
                                               {**payload, 'assessment_perspective': cfg.get('assessment_perspective', {}), 'source_block_lengths': lengths, 'repair': repair}, schema, stage)
                if stage == 'dimension':
                    repairs = normalize_dimension_container(
                        value, payload['rubric']['route'],
                        payload['rubric']['route'] in cfg.get('lexical_skip_routes', []),
                        payload['rubric']['dimensions'][0]['id'] in cfg.get('counted_dimensions', {}).get(payload['rubric']['route'], []),
                        payload['rubric']['dimensions'][0].get('projection'))
                    if repairs:
                        entry['container_repairs'] = repairs
                        entry['relocated_container_fields'] = [key for key in ('sense_matches', 'notes') if f'moved {key} to response' in repairs]
                validate(value, schema)
                aligned = align_unique_quotes(value, blocks)
                if stage == 'structure':
                    by_id = {u['id']:u for u in value['units']}
                    for selector in value['coverage']:
                        u=by_id.get(selector['unit_id']);key=selector['block_id'];start=selector['start'];end=selector['end']
                        if u is not None and key in blocks and 0 <= start < end <= len(blocks[key]):
                            if not any(r['block_id']==key and r['start']<=start and end<=r['end'] for r in u['refs']):
                                u['refs'].append({'block_id':key,'start':start,'end':end,'quote':blocks[key][start:end]})
                check_refs(value, blocks)
                if extra:
                    extra(value)
                if payload.get('_dimension_request'):
                    entry['dimension_id'] = payload['rubric']['dimensions'][0]['id']
                    entry['output'] = copy.deepcopy(value)
                self.trace.append({**entry, 'outcome': 'ok', 'offset_repairs': aligned})
                return value
            except (ContractError, ProviderError) as exc:
                last, previous = exc, value
                self.trace.append({**entry, 'outcome': 'failed', 'reason': str(exc), 'output': value})
                if isinstance(exc, ProviderError) and not exc.retryable:
                    self.provider_blocked = str(exc)
                    break
        raise ProviderError(f'{stage}: {last}', False)

    def run_document(self, document):
        try:
            validate(document, self.bundle.schemas['input'])
            unique(document['blocks'], 'blocks')
            require(bool(document['blocks']), 'empty input')
            blocks = {b['id']: b['text'] for b in document['blocks']}
            require(sum(map(len, blocks.values())) <= self.bundle.config['limits']['max_input_characters'], 'input exceeds limit')
        except ContractError as exc:
            return {'id': document.get('id') if isinstance(document, dict) else None, 'status': 'unavailable', 'reason': str(exc), 'units': [], 'source': document}
        try:
            structure = self.node('structure', {'document': document}, blocks,
                                  lambda data: check_structure(data, blocks, self.bundle.config['limits']['max_units_per_document']))
        except ProviderError as exc:
            return {'id': document['id'], 'status': 'unavailable', 'reason': str(exc), 'units': [], 'source': document}
        results = {}
        # Children finish first; whole scores use their evidence, never their mean stars.
        ordered = sorted(structure['units'], key=lambda u: u['grain'] == 'whole')
        for unit in ordered:
            base = {'unit_id': unit['id'], 'grain': unit['grain'], 'parent_id': unit['parent_id'], 'input_missing': unit['missing']}
            completed_stages = {}
            if unit['grain'] == 'unknown':
                results[unit['id']] = {**base, 'status': 'unavailable', 'reason': unit['reason']}
                continue
            try:
                local_blocks = {r['block_id']: blocks[r['block_id']] for r in unit['refs']}
                def refs_in_unit(value):
                    def visit(v):
                        if isinstance(v, dict):
                            if {'block_id', 'start', 'end', 'quote'} <= v.keys():
                                require(any(r['block_id'] == v['block_id'] and r['start'] <= v['start'] < v['end'] <= r['end'] for r in unit['refs']), 'reference outside unit context')
                            for child in v.values(): visit(child)
                        elif isinstance(v, list):
                            for child in v: visit(child)
                    visit(value)
                context = {'unit': unit, 'blocks': local_blocks}
                def route_check(data):
                    refs_in_unit(data)
                    rid = data['route']
                    require(rid is None or rid in self.bundle.rubrics, 'unsupported route id')
                    if rid:
                        require(self.bundle.rubrics[rid]['grain'] == unit['grain'], 'route grain mismatch')
                        require(data['refs'], 'route has no evidence')
                routing = self.node('route', {**context, 'routes': self.bundle.routes}, local_blocks, route_check)
                completed_stages['routing'] = routing
                if routing['route'] is None:
                    results[unit['id']] = {**base, 'status': 'unsupported', 'routing': routing, 'reason': routing['reason']}
                    continue
                rubric = self.bundle.rubrics[routing['route']]
                tokens = token_selectors(unit['refs'])
                tokens_by_id = {t['id']:t['ref'] for t in tokens}
                def lexical_check(data):
                    unique(data['occurrences'], 'occurrences')
                    for occurrence in data['occurrences']:
                        require(occurrence['id'] in tokens_by_id, 'unknown lexical token id')
                        occurrence['ref'] = tokens_by_id[occurrence['id']]
                        if occurrence['exemption'] == 'null':
                            occurrence['exemption'] = None
                    if {o['id'] for o in data['occurrences']} != set(tokens_by_id):
                        data['complete'] = False
                        data['coverage_note'] += '；程序发现词汇出现未完整覆盖'
                    require(data['occurrences'] or not data['complete'], 'empty lexical evidence cannot be complete')
                try:
                    if routing['route'] in self.bundle.config.get('lexical_skip_routes',[]):
                        lexical = {'occurrences': [], 'complete': False, 'coverage_note': '该题型没有词频或熟词僻义计数维度，按规则跳过全量词汇提取；语义理解仍由证据节点完成'}
                    else:
                        lexical = self.node('lexical', {**context, 'tokens': tokens}, local_blocks, lexical_check)
                except ProviderError as exc:
                    lexical = {'occurrences': [], 'complete': False, 'coverage_note': str(exc)}
                lookup = self.bundle.lookup(lexical)
                completed_stages['lexical'] = lookup
                children = [results[c['id']] for c in ordered if c['parent_id'] == unit['id']]
                def evidence_check(data, expected_rubric):
                    check_dimensions(data, expected_rubric)
                    refs_in_unit(data)
                    for dim in data['dimensions']:
                        unique(dim['facts'], 'facts')
                        require(dim['scope_reason'].strip(), 'missing sentence scope explanation')
                        require(dim['complete_sentence_refs'] or dim['missing'], 'missing complete sentence evidence')
                        require(dim['facts'] or dim['missing'], 'dimension without evidence or missing reason')
                        for fact in dim['facts']:
                            require(fact['refs'], 'fact without source')
                        if 'passage_refs' in dim['metrics']:
                            ref_schema = self.bundle.schemas['route']['properties']['refs']
                            validate(dim['metrics']['passage_refs'], ref_schema)
                            # Validate the projection contract before accepting the node.
                            rule = next(r for r in rubric['dimensions'] if r['id'] == dim['id'])
                            if rule.get('projection') == 'seven_length':
                                project_dimension(rule, dim, self.bundle.config['scoring'])
                        rule = next(r for r in rubric['dimensions'] if r['id'] == dim['id'])
                        if rule.get('projection') == 'grammar_distractors':
                            options = dim['metrics'].get('option_evaluations', [])
                            require(type(options) is list, 'option evaluations must be an array')
                            for option in options:
                                require(type(option) is dict and 'refs' in option, 'option evidence missing refs')
                                validate(option['refs'], self.bundle.schemas['route']['properties']['refs'])
                            score, reason = project_dimension(rule,dim,self.bundle.config['scoring'])
                            if score is None and not dim['missing']:
                                dim['missing'].append(reason)
                        if rule.get('projection') == 'cloze_span' and not dim['missing']:
                            items = dim['metrics'].get('clue_items')
                            require(type(items) is list and bool(items), 'missing per-item clue evidence')
                            child_ids = {c['unit_id'] for c in children}
                            ids = []
                            scopes = ('same_sentence', 'adjacent_sentence', 'cross_paragraph', 'global')
                            for item in items:
                                require(type(item) is dict and set(item) == {'item_id','scope','refs'}, 'invalid clue item')
                                require(type(item['item_id']) is str and item['scope'] in scopes, 'invalid clue classification')
                                validate(item['refs'], self.bundle.schemas['route']['properties']['refs'])
                                require(item['refs'], 'clue item without source')
                                ids.append(item['item_id'])
                            require(len(ids) == len(set(ids)) and set(ids) == child_ids, 'clue items do not cover children')
                            dim['metrics']['total_items'] = len(items)
                            for scope in scopes:
                                dim['metrics'][scope + '_items'] = sum(item['scope'] == scope for item in items)
                    self.bundle.senses(lookup, data['sense_matches'])
                # Each horizontal node owns evidence, judgment and rationale together.
                # No evidence-stage or score-stage model call follows this decision.
                dimensions, evidence_dimensions, findings, notes = [], [], [], []
                completed_stages['dimension_nodes'] = {}
                requirements = self.bundle.config['resource_requirements'].get(routing['route'], {})
                evidence_keys = self.bundle.schemas['evidence']['properties']['dimensions']['items']['properties']
                score_keys = self.bundle.schemas['score']['properties']['dimensions']['items']['properties']
                for rule in rubric['dimensions']:
                    one_rubric = {**rubric, 'dimensions': [rule]}
                    one_rubric.pop('source_rows', None)
                    needs = requirements.get(rule['id'], [])
                    def dimension_check(data):
                        evidence_check(data, one_rubric)
                        dim = data['dimensions'][0]
                        local_findings = self.bundle.senses(lookup, data['sense_matches'])
                        if 'lexical_complete' in needs and not lookup['complete']:
                            dim['missing'].append('词汇提取未完整覆盖')
                        if 'contextual_senses' in needs:
                            judged = {f['occurrence_id'] for f in local_findings if f['rare_sense'] != 'unknown'}
                            required = {o['id'] for o in lookup['occurrences'] if o['in_vocabulary']}
                            if not required <= judged:
                                dim['missing'].append('部分课标词语境义项尚未判断或确实存在歧义')
                        require(set(dim['evidence_ids']) <= {f['id'] for f in dim['facts']}, 'unknown evidence id')
                        if not dim['missing']:
                            project_counted_syntax(dim, rule, self.bundle.config.get('syntax_count_checks', {}).get(routing['route']))
                            if rule.get('projection'):
                                projected, projection_explanation = project_dimension(rule, dim, self.bundle.config['scoring'])
                                apply_existing_projection(dim, rule, projected, projection_explanation)
                        align_original_anchor(dim, rule)
                        expected = None if dim['missing'] else dim['score']
                        if rule.get('projection'):
                            expected, projection_reason = project_dimension(rule, dim, self.bundle.config['scoring'])
                        else:
                            projection_reason = '缺少必要证据：'+'；'.join(dim['missing']) if dim['missing'] else '模型按当前维度规则判档'
                        require(dim['score'] == expected,
                                f'dimension score contradicts its own evidence: expected {expected}; {projection_reason}. Repair the complete dimension judgment and explanation together.')
                        basis = dim['score_basis']
                        if 'judgment' in self.bundle.prompts:
                            audit = dim['metrics'].get('rule_application')
                            require(isinstance(audit,dict), 'metrics.rule_application required: copy the selected original anchor and explain criterion evidence')
                            require(audit.get('anchor_score') == dim['score'], 'rule_application anchor_score differs from score')
                            anchor = rule['anchors'][dim['score']-1] if dim['score'] is not None else None
                            require(audit.get('anchor_quote') == anchor, f'rule_application anchor_quote must copy the original anchor exactly: {anchor!r}')
                            require(all(isinstance(audit.get(k),str) and audit[k].strip() for k in ('criterion_evidence','adjacent_band_check')), 'empty rule_application explanation')
                            if 'judgment_items' in dim['metrics']:
                                items=dim['metrics']['judgment_items']
                                require(isinstance(items,list), 'judgment_items must be a list')
                                unique(items,'judgment_items')
                                for item in items:
                                    require(type(item.get('counted')) is bool and item.get('label') and item.get('reason') and item.get('refs'), 'judgment item requires counted boolean, label, reason and source refs')
                                    validate(item['refs'],self.bundle.schemas['route']['properties']['refs'])
                                require(type(dim['metrics'].get('judgment_count')) is int and dim['metrics']['judgment_count']==sum(i['counted'] for i in items), 'judgment_count differs from explicitly counted items')
                                count_check = self.bundle.config.get('syntax_count_checks',{}).get(routing['route'],{})
                                if count_check.get('dimension') == dim['id'] and dim['score'] is not None:
                                    count = dim['metrics']['judgment_count']
                                    for band in count_check['bands']:
                                        if count >= band['min'] and (band['max'] is None or count <= band['max']):
                                            require(dim['score']==band['score'], f"Declared syntax count {count} maps to {band['score']} under this original rubric. Repair the same complete judgment; do not invent a different threshold.")
                        require(basis['evaluated_score'] == dim['score'], 'score_basis must explain the same score; repair the complete dimension output')
                        require(all(basis[k].strip() for k in ('student_demand','anchor_match','why_this_score')), 'empty score_basis explanation')
                        require(dim['reason'].strip(), 'empty dimension reason')
                        if dim['score'] is not None:
                            require(dim['refs'] and dim['evidence_ids'] and basis['refs'], 'score without evidence')
                    try:
                        value = self.node('dimension', {**context, '_dimension_request': True,
                            'rubric': one_rubric, 'general_rules': self.bundle.resources['general_rules'],
                            'lexical_lookup': lookup, 'rare_examples': self.bundle.resources['rare_examples'],
                            'rare_sense_policy': self.bundle.config['lexical'], 'children': children,
                            'resource_requirements': needs, 'projection_config': self.bundle.config['scoring']},
                            local_blocks, dimension_check)
                        completed_stages['dimension_nodes'][rule['id']] = value
                        result = value['dimensions'][0]
                        findings.extend({**f, 'dimension_id': rule['id']} for f in self.bundle.senses(lookup, value['sense_matches']))
                        notes.extend(value['notes'])
                        ev = {k: copy.deepcopy(result[k]) for k in evidence_keys}
                        dim = {k: copy.deepcopy(result[k]) for k in score_keys}
                        dim['complete_sentence_refs'] = copy.deepcopy(result['complete_sentence_refs'])
                        dim['scope_reason'] = result['scope_reason']
                        dim['score_basis']['final_score'] = dim['score']
                        dim['score_basis']['score_alignment'] = 'aligned'
                        if rule.get('projection'):
                            _, dim['projection_reason'] = project_dimension(rule, result, self.bundle.config['scoring'])
                    except (ProviderError, ContractError) as exc:
                        # An invalid dimension cannot publish a contradictory score.
                        # Keep raw attempts in trace and continue other dimensions.
                        reason = str(exc)
                        ev = {'id':rule['id'], 'facts':[], 'metrics':{}, 'missing':[reason],
                              'complete_sentence_refs':[], 'scope_reason':'当前维度未通过合同校验'}
                        dim = {'id':rule['id'], 'score':None, 'reason':reason, 'refs':[], 'evidence_ids':[],
                               'complete_sentence_refs':[], 'scope_reason':ev['scope_reason'],
                               'score_basis':{'evaluated_score':None, 'final_score':None, 'score_alignment':'unavailable',
                                   'student_demand':'未形成有效判断', 'anchor_match':'未形成有效判断',
                                   'why_this_score':reason, 'refs':[]}}
                        notes.append(f"{rule['id']}节点失败，已保留日志并继续其他维度")
                    dimensions.append(dim)
                    evidence_dimensions.append(ev)
                evidence = {'dimensions': evidence_dimensions, 'sense_matches': [], 'notes': notes}
                # Per-dimension sense conclusions remain attached to their node snapshots.
                completed_stages['evidence'] = evidence
                completed_stages['sense_findings'] = findings
                calculated = aggregate(dimensions, rubric, self.bundle.config['scoring'])
                if unit['missing'] and calculated['status'] == 'complete':
                    calculated['status'] = 'partial'
                results[unit['id']] = {**base, **calculated, 'route': routing['route'], 'routing': routing,
                                       'dimensions': dimensions, 'evidence': evidence, 'lexical': lookup, 'sense_findings': findings, 'notes': notes, 'dimension_nodes': completed_stages['dimension_nodes']}
            except (ProviderError, ContractError) as exc:
                results[unit['id']] = {**base, 'status': 'unavailable', 'reason': str(exc), 'completed_stages': completed_stages}
        return {'id': document['id'], 'status': 'finished', 'source': document, 'structure': structure,
                'units': [results[u['id']] for u in structure['units']]}

    def run(self, documents):
        results = [self.run_document(d) for d in documents]
        counts = {}
        for document in results:
            for unit in document['units']:
                counts[unit['status']] = counts.get(unit['status'], 0) + 1
        return {'status': 'finished', 'versions': self.bundle.snapshot(), 'documents': results,
                'summary': {'documents': len(results), 'document_failures': sum(d['status'] == 'unavailable' for d in results),
                            'unit_statuses': counts, 'node_attempts': self.attempts, 'network_calls': self.provider.calls},
                'trace': self.trace, 'usage': self.provider.usage}
