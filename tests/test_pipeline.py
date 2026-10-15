import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from english_difficulty.contracts import ContractError, check_refs, check_structure, validate, align_unique_quotes
from english_difficulty.resources import Bundle
from english_difficulty.scoring import aggregate, project_dimension, word_count
from english_difficulty.pipeline import Pipeline
from english_difficulty.provider import ArkMini, ProviderError
from english_difficulty.ark_schema import to_strict_schema
from english_difficulty.cli import load_input

ROOT = Path(__file__).resolve().parents[1]
TEXT = 'Yesterday Tom went to school. Choose the correct answer.'
REF = {'block_id': 'b1', 'start': 0, 'end': len(TEXT), 'quote': TEXT}
DOC = {'id': 'synthetic-only', 'blocks': [{'id': 'b1', 'text': TEXT}]}

def unit(uid='u1', grain='small', parent=None):
    return {'id': uid, 'grain': grain, 'parent_id': parent, 'refs': [REF], 'missing': [], 'reason': 'synthetic transport fixture'}

class FakeProvider:
    """Deterministic contract fixture. Never evidence of model accuracy."""
    def __init__(self, bundle, units=None, routes=None, mutations=None):
        self.bundle = bundle
        self.units = units or [unit()]
        self.routes = routes or {}
        self.mutations = mutations or {}
        self.calls = 0
        self.usage = []
        self.history = []
        self.systems = []

    def complete(self, system, payload, schema, node):
        self.calls += 1
        self.history.append((node, payload))
        self.systems.append(system)
        uid = payload.get('unit', {}).get('id', '')
        if node == 'structure':
            data = {'units': copy.deepcopy(self.units), 'coverage': [{'block_id': 'b1', 'start': 0, 'end': len(TEXT), 'unit_id': self.units[0]['id'], 'role': 'question'}], 'issues': []}
        elif node == 'route':
            data = {'route': self.routes.get(uid, 'grammar_choice'), 'reason': 'synthetic', 'refs': [REF]}
        elif node == 'lexical':
            data = {'occurrences': [{'id': t['id'], 'lemma': 'the', 'pos': 'article', 'contextual_sense': 'synthetic', 'exemption': None} for t in payload['tokens']], 'complete': True, 'coverage_note': 'fixture only'}
        elif node == 'dimension':
            # Construct a single atomic response. Legacy mutation hooks below
            # exercise existing regression fixtures, not production calls.
            rules = self.bundle.rubrics[payload['rubric']['route']]['dimensions']
            data = {'dimensions': [], 'sense_matches': [], 'notes': []}
            for rule in rules:
                metrics = {'option_evaluations': [], 'distinction': None}
                if rule.get('projection') == 'grammar_distractors':
                    metrics = {'option_evaluations':[{'is_correct':i==0,'temporarily_valid':i<2,'requires_rule_elimination':i>0,'reason':'synthetic','refs':[REF]} for i in range(4)],'distinction':None}
                data['dimensions'].append({'id':rule['id'], 'facts':[{'id':'f1','statement':'synthetic','refs':[REF]}],
                    'metrics':metrics,'missing':[],'complete_sentence_refs':[copy.deepcopy(REF)],'scope_reason':'synthetic full context'})
            data['sense_matches'] = [{'occurrence_id':o['id'],'sense_id':None,'basis':'contextual_model','example_lemma':None,'common_sense':'synthetic usual','contextual_sense':'synthetic usual','judgment':'common','reason':'synthetic contextual comparison','ref':o['ref']} for o in payload['lexical_lookup']['occurrences'] if o['in_vocabulary']]
            callback = self.mutations.get(('evidence',uid),self.mutations.get('evidence'))
            if callback: data = callback(data,payload)
            for dim,rule in zip(data['dimensions'],rules):
                if rule.get('projection') == 'cloze_span' and dim['metrics'].get('clue_items'):
                    items = dim['metrics']['clue_items']
                    dim['metrics']['total_items'] = len(items)
                    for scope in ('same_sentence','adjacent_sentence','cross_paragraph','global'):
                        dim['metrics'][scope+'_items'] = sum(i['scope']==scope for i in items)
                needs=self.bundle.config['resource_requirements'].get(payload['rubric']['route'],{}).get(rule['id'],[])
                if 'contextual_senses' in needs and not data['sense_matches']:
                    dim['missing'].append('synthetic missing senses')
                score=project_dimension(rule,dim,self.bundle.config['scoring'])[0] if rule.get('projection') else (None if dim['missing'] else 2)
                if score is None: dim['missing'].append('synthetic missing evidence')
                dim.update(score=score,reason='synthetic',refs=[REF],evidence_ids=['f1'],score_basis={
                    'evaluated_score':score,'student_demand':'synthetic student demand','anchor_match':'synthetic anchor',
                    'why_this_score':'synthetic explanation','refs':[copy.deepcopy(REF)]})
                dim['metrics']['rule_application']={'anchor_score':score,'anchor_quote':rule['anchors'][score-1] if score else None,'criterion_evidence':'synthetic evidence','adjacent_band_check':'synthetic boundary'}
                dim['metrics'].update(judgment_items=[],judgment_count=0)
                if rule['id'] in self.bundle.config.get('counted_dimensions',{}).get(payload['rubric']['route'],[]):
                    dim['metrics'].update(judgment_items=[{'id':'j1','label':'synthetic','refs':[REF],'counted':True,'reason':'synthetic'}],judgment_count=1)
                    if payload['rubric']['route']=='grammar_choice' and rule['id']=='d2':
                        dim['metrics']['judgment_items'][0]['counted']=False
                        dim['metrics']['judgment_count']=0
            callback=self.mutations.get(('score',uid),self.mutations.get('score'))
            if callback: data=callback(data,payload)
            wanted=payload['rubric']['dimensions'][0]['id']
            data['dimensions']=[d for d in data['dimensions'] if d['id']==wanted]
            callback=self.mutations.get(('dimension',uid),self.mutations.get('dimension'))
            return callback(data,payload) if callback else data
        callback = self.mutations.get((node, uid), self.mutations.get(node))
        return callback(data, payload) if callback else data

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.bundle = Bundle(ROOT)

    def test_dimensionwise_preserves_scores_and_complete_input(self):
        base = Pipeline(self.bundle, FakeProvider(self.bundle)).run([DOC])
        fake = FakeProvider(self.bundle)
        actual = Pipeline(self.bundle, fake).run([DOC])
        self.assertEqual(actual['documents'][0]['units'][0]['score'],base['documents'][0]['units'][0]['score'])
        parts = [p for n,p in fake.history if n == 'dimension']
        self.assertEqual(len(parts),5)
        self.assertFalse(any(n in ('evidence','score') for n,p in fake.history))
        self.assertTrue(all(len(p['rubric']['dimensions'])==1 for p in parts))
        self.assertTrue(all(p['blocks']['b1']==TEXT for p in parts))

    def test_import_inventory_and_frequency_edges(self):
        self.assertEqual(len(self.bundle.routes), 11)
        self.assertEqual(sum(len(x['dimensions']) for x in self.bundle.rubrics.values()), 58)
        entries = self.bundle.resources['vocabulary']['entries']
        self.assertEqual(len(entries), 3500)
        self.assertEqual(sum(e['frequency'] > 26 for e in entries), 1758)
        self.assertEqual(sum(e['frequency'] < 10 for e in entries), 881)
        for freq, label in [(9, 'low'), (10, 'middle'), (26, 'middle'), (27, 'high')]:
            self.bundle.vocabulary['test'] = [{'lemma': 'test', 'frequency': freq}]
            actual = self.bundle.lookup({'occurrences': [{'id': 'w', 'lemma': 'test', 'pos': 'noun', 'exemption': None}], 'complete': True, 'coverage_note': ''})
            self.assertEqual(actual['occurrences'][0]['frequency_class'], label)

    def test_same_dimension_retries_complete_judgment(self):
        count = [0]
        def mismatch_once(data, payload):
            if data['dimensions'][0]['id'] == 'd4':
                count[0] += 1
                if count[0] == 1:
                    data['dimensions'][0]['score_basis']['student_demand'] = ''
            return data
        fake = FakeProvider(self.bundle, mutations={'dimension':mismatch_once})
        out = Pipeline(self.bundle, fake).run([DOC])
        self.assertEqual(out['documents'][0]['units'][0]['status'], 'complete')
        retries = [p for n,p in fake.history if n == 'dimension' and p['rubric']['dimensions'][0]['id']=='d4']
        self.assertEqual(len(retries),2)
        self.assertIn('empty score_basis explanation',retries[1]['repair']['validation_error'])
        self.assertIn('facts',retries[1]['repair']['previous_output']['dimensions'][0])
        retry_prompts=[s for (n,p),s in zip(fake.history,fake.systems) if n=='dimension' and p['rubric']['dimensions'][0]['id']=='d4']
        self.assertIn('先读取repair.validation_error',retry_prompts[1])
        self.assertFalse(any(n in ('evidence','score') for n,p in fake.history))

    def test_empty_fact_reference_is_not_filled_with_full_context(self):
        def missing_ref(data,payload):
            if data['dimensions'][0]['id']=='d2':
                data['dimensions'][0]['facts'][0]['refs']=[]
            return data
        fake=FakeProvider(self.bundle,mutations={'dimension':missing_ref})
        out=Pipeline(self.bundle,fake).run([DOC])
        u=out['documents'][0]['units'][0]
        self.assertEqual(u['status'],'partial')
        self.assertEqual(u['missing_dimensions'],['d2'])
        self.assertEqual(u['dimensions'][-1]['score'],2)

    def test_misplaced_container_fields_move_without_changing_judgment(self):
        def nested(data,payload):
            for key in ('sense_matches','notes'):
                data['dimensions'][0][key]=data.pop(key)
            return data
        fake=FakeProvider(self.bundle,mutations={'dimension':nested})
        result=Pipeline(self.bundle,fake).run([DOC])
        self.assertEqual(result['documents'][0]['units'][0]['status'],'complete')
        self.assertEqual(fake.calls,7)
        entries=[t for t in result['trace'] if t['stage']=='dimension']
        self.assertTrue(all(t['relocated_container_fields']==['sense_matches','notes'] for t in entries))

    def test_judgment_count_is_derived_from_explicit_list(self):
        def wrong_count(data,payload):
            d=data['dimensions'][0]
            if d['id']=='d2':
                d['metrics']['judgment_items']=[{'id':'j1','label':'synthetic','refs':[REF],'counted':True,'reason':'synthetic'}]
                d['metrics']['judgment_count']=2
            return data
        out=Pipeline(self.bundle,FakeProvider(self.bundle,mutations={'dimension':wrong_count})).run([DOC])
        unit=out['documents'][0]['units'][0]
        self.assertEqual(unit['status'],'complete')
        self.assertEqual(unit['dimensions'][1]['score'],3)
        metrics=unit['dimension_nodes']['d2']['dimensions'][0]['metrics']
        self.assertEqual(metrics['judgment_count'],1)
        self.assertEqual(metrics['model_suggested_count'],2)

    def test_wrong_original_anchor_is_replaced_from_rubric_with_audit(self):
        def wrong_anchor(data,payload):
            d=data['dimensions'][0]
            if d['id']=='d2':d['metrics']['rule_application']['anchor_quote']='invented merged band'
            return data
        out=Pipeline(self.bundle,FakeProvider(self.bundle,mutations={'dimension':wrong_anchor})).run([DOC])
        unit=out['documents'][0]['units'][0]
        self.assertEqual(unit['status'],'complete')
        metrics=unit['dimension_nodes']['d2']['dimensions'][0]['metrics']
        self.assertEqual(metrics['model_suggested_anchor_quote'],'invented merged band')
        self.assertEqual(metrics['rule_application']['anchor_quote'],self.bundle.rubrics['grammar_choice']['dimensions'][1]['anchors'][unit['dimensions'][1]['score']-1])

    def test_two_declared_structures_project_to_original_band(self):
        def wrong_band(data,payload):
            d=data['dimensions'][0]
            if d['id']=='d2':
                d['metrics']['judgment_items']=[{'id':f'j{i}','label':'synthetic','refs':[REF],'counted':True,'reason':'synthetic'} for i in (1,2)]
                d['metrics']['judgment_count']=2
                d['score']=3
                d['score_basis']['evaluated_score']=3
                d['metrics']['rule_application'].update(anchor_score=3,anchor_quote=self.bundle.rubrics['grammar_choice']['dimensions'][1]['anchors'][2])
            return data
        out=Pipeline(self.bundle,FakeProvider(self.bundle,mutations={'dimension':wrong_band})).run([DOC])
        unit=out['documents'][0]['units'][0]
        self.assertEqual(unit['status'],'complete')
        self.assertEqual(unit['dimensions'][1]['score'],4)
        metrics=unit['dimension_nodes']['d2']['dimensions'][0]['metrics']
        self.assertEqual(metrics['model_suggested_score'],3)
        self.assertEqual(metrics['judgment_count'],2)
        self.assertEqual(metrics['rule_application']['anchor_quote'],self.bundle.rubrics['grammar_choice']['dimensions'][1]['anchors'][3])

    def test_empty_transport_fields_are_repaired_without_creating_evidence(self):
        def broken_container(data,payload):
            if data['dimensions'][0]['id']=='d2':
                data.pop('notes')
                data.pop('sense_matches')
            return data
        out=Pipeline(self.bundle,FakeProvider(self.bundle,mutations={'dimension':broken_container})).run([DOC])
        self.assertEqual(out['documents'][0]['units'][0]['status'],'complete')
        repaired=[t for t in out['trace'] if t.get('dimension_id')=='d2' and t['outcome']=='ok'][0]
        self.assertIn('added empty notes',repaired['container_repairs'])
        self.assertIn('added empty sense_matches for lexical-free route',repaired['container_repairs'])

    def test_reading_three_cited_structures_map_to_four_without_rewriting_items(self):
        def three_structures(data,payload):
            dim=data['dimensions'][0]
            if dim['id']=='d4':
                dim['metrics']['judgment_items']=[{'id':f'j{i}','label':f'synthetic structure {i}',
                    'refs':[REF],'counted':True,'reason':'synthetic independent structure'} for i in (1,2,3)]
                dim['metrics']['judgment_count']=3
            return data
        fake=FakeProvider(self.bundle,routes={'u1':'reading_single'},mutations={'dimension':three_structures})
        unit=Pipeline(self.bundle,fake).run([DOC])['documents'][0]['units'][0]
        self.assertEqual(unit['dimensions'][3]['score'],4)
        self.assertEqual(unit['dimension_nodes']['d4']['dimensions'][0]['metrics']['model_suggested_score'],2)
        self.assertEqual(len(unit['dimension_nodes']['d4']['dimensions'][0]['metrics']['judgment_items']),3)

    def test_misplaced_empty_grammar_defaults_are_not_sent_to_reading_dimension(self):
        fake=FakeProvider(self.bundle,routes={'u1':'reading_single'})
        def misplaced(data,payload):
            if data['dimensions'][0]['id']=='d4':
                data['dimensions'][0]['option_evaluations']=[]
                data['dimensions'][0]['distinction']=None
            return data
        fake.mutations['dimension']=misplaced
        out=Pipeline(self.bundle,fake).run([DOC])
        self.assertEqual(out['documents'][0]['units'][0]['status'],'complete')
        self.assertNotIn('option_evaluations',out['documents'][0]['units'][0]['dimension_nodes']['d4']['dimensions'][0])

    def test_noncount_dimension_drops_unused_band_checklist(self):
        def unnecessary_list(data,payload):
            if data['dimensions'][0]['id']=='d2':
                data['dimensions'][0]['metrics']['judgment_items']=[{'id':'j1','label':'synthetic band candidate',
                    'refs':[{'block_id':'b1','start':0,'end':len(TEXT)}],
                    'counted':True,'reason':'synthetic'}]
                data['dimensions'][0]['metrics']['judgment_count']=1
            return data
        fake=FakeProvider(self.bundle,routes={'u1':'reading_single'},mutations={'dimension':unnecessary_list})
        out=Pipeline(self.bundle,fake).run([DOC])
        self.assertEqual(out['documents'][0]['units'][0]['status'],'complete')
        metrics=out['documents'][0]['units'][0]['dimension_nodes']['d2']['dimensions'][0]['metrics']
        self.assertEqual((metrics['judgment_items'],metrics['judgment_count']),([],0))

    def test_dimension_guidance_is_scoped_to_its_own_task(self):
        fake=FakeProvider(self.bundle)
        Pipeline(self.bundle,fake).run([DOC])
        prompts={p['rubric']['dimensions'][0]['id']:s for (n,p),s in zip(fake.history,fake.systems) if n=='dimension'}
        self.assertIn('## 句法结构维度',prompts['d2'])
        self.assertNotIn('## 干扰排除维度',prompts['d2'])
        self.assertIn('## 考点呈现形式与综合度',prompts['d1'])
        self.assertNotIn('## 分析步骤维度',prompts['d1'])
        self.assertIn('## 分析步骤维度',prompts['d5'])

    def test_reading_cognition_and_paraphrase_have_distinct_prompts(self):
        fake=FakeProvider(self.bundle,routes={'u1':'reading_single'})
        Pipeline(self.bundle,fake).run([DOC])
        prompts={p['rubric']['dimensions'][0]['id']:s for (n,p),s in zip(fake.history,fake.systems) if n=='dimension'}
        self.assertIn('阅读单题：题型认知层级',prompts['d1'])
        self.assertIn('阅读单题：同义改写深度',prompts['d3'])
        self.assertNotIn('阅读单题：同义改写深度',prompts['d1'])
        self.assertNotIn('阅读单题：题型认知层级',prompts['d3'])
        self.assertIn('阅读单题：定位难度',prompts['d2'])

    def test_every_route_uses_one_complete_node_per_dimension(self):
        for rid,rubric in self.bundle.rubrics.items():
            with self.subTest(route=rid):
                fake=FakeProvider(self.bundle,[unit(grain=rubric['grain'])],{'u1':rid})
                Pipeline(self.bundle,fake).run([DOC])
                calls=[p for n,p in fake.history if n=='dimension']
                self.assertEqual({p['rubric']['dimensions'][0]['id'] for p in calls},{r['id'] for r in rubric['dimensions']})
                self.assertTrue(all(len(p['rubric']['dimensions'])==1 and p['blocks']['b1']==TEXT for p in calls))
                self.assertFalse(any(n in ('evidence','score') for n,p in fake.history))

    def test_exact_star_and_three_band_boundaries(self):
        for score, star in [('1', 1), ('1.80999', 1), ('1.81', 2), ('2.61', 3), ('3.41', 4), ('4.21', 5), ('5', 5)]:
            from decimal import Decimal
            v = Decimal(score)
            rubric = {'dimensions': [{'id': 'a', 'weight': str((5-v)/4)}, {'id': 'b', 'weight': str((v-1)/4)}]}
            result = aggregate([{'id': 'a', 'score': 1}, {'id': 'b', 'score': 5}], rubric, self.bundle.config['scoring'])
            self.assertEqual(result['stars'], star)
        for score, band in [('2.329', '易'), ('2.33', '中'), ('3.66', '中'), ('3.661', '难')]:
            from decimal import Decimal
            v = Decimal(score)
            r = {'dimensions': [{'id': 'a', 'weight': str((5-v)/4)}, {'id': 'b', 'weight': str((v-1)/4)}]}
            self.assertEqual(aggregate([{'id': 'a', 'score': 1}, {'id': 'b', 'score': 5}], r, self.bundle.config['scoring'])['three_band'], band)

    def test_missing_renormalization_and_all_missing(self):
        r = {'dimensions': [{'id': 'a', 'weight': .3}, {'id': 'b', 'weight': .7}]}
        out = aggregate([{'id': 'a', 'score': 4}, {'id': 'b', 'score': None}], r, self.bundle.config['scoring'])
        self.assertEqual((out['score'], out['status'], out['available_weight']), ('4', 'partial', .3))
        out = aggregate([{'id': 'a', 'score': None}, {'id': 'b', 'score': None}], r, self.bundle.config['scoring'])
        self.assertEqual(out['status'], 'unavailable')
        self.assertIsNone(out['stars'])

    def test_seven_length_all_boundaries(self):
        d = self.bundle.rubrics['seven_whole']['dimensions'][3]
        for n, expected in [(180,1),(181,2),(200,2),(201,3),(249,3),(250,4),(299,4),(300,5),(301,5)]:
            text = ' '.join(['word']*n)
            ev = {'missing': [], 'metrics': {'passage_refs': [{'block_id':'b', 'start':0, 'end':len(text), 'quote':text}]}}
            self.assertEqual(project_dimension(d, ev, self.bundle.config['scoring'])[0], expected)
        self.assertEqual(word_count("Don't re-read _____ 123 Tom’s book."), 4)

    def test_cloze_description_replaces_discarded_ranges(self):
        d = self.bundle.rubrics['cloze_whole']['dimensions'][2]
        def score(total, same, adj, cross, glob=0):
            return project_dimension(d, {'missing': [], 'metrics': dict(zip(['total_items','same_sentence_items','adjacent_sentence_items','cross_paragraph_items','global_items'],[total,same,adj,cross,glob]))}, self.bundle.config['scoring'])[0]
        self.assertEqual(score(15,9,0,6), 4)
        self.assertEqual(score(15,15,0,0), 1)
        self.assertEqual(score(15,13,2,0), 2)
        self.assertEqual(score(15,10,2,3), 3)
        self.assertEqual(score(15,7,0,8), 5)
        self.assertIsNone(score(100,54,0,46))
        self.assertIsNone(score(15,10,0,2))
        self.assertIsNone(score(100,90,0,10))  # band 2 requires rest adjacent

    def test_full_flow_structure_is_first(self):
        fake = FakeProvider(self.bundle)
        out = Pipeline(self.bundle, fake).run([DOC])
        self.assertEqual(out['documents'][0]['units'][0]['status'], 'complete')
        self.assertEqual([n for n,p in fake.history], ['structure','route']+['dimension']*5)
        self.assertEqual(out['documents'][0]['source'], DOC)

    def test_basis_uses_final_projection_without_changing_mapping(self):
        fake = FakeProvider(self.bundle)
        out = Pipeline(self.bundle, fake).run([DOC])
        d = out['documents'][0]['units'][0]['dimensions'][3]
        self.assertEqual(d['score_basis']['evaluated_score'], d['score'])
        self.assertEqual(d['score'], 2)
        self.assertEqual(d['score_basis']['refs'], [REF])
        self.assertIn('K12', fake.history[0][1]['assessment_perspective']['population'])

    def test_short_explanation_citation_cannot_truncate_saved_scope(self):
        def truncate(d, p):
            d['dimensions'][1]['score_basis']['refs'] = [{**REF,'end':9,'quote':TEXT[:9]}]
            return d
        out = Pipeline(self.bundle, FakeProvider(self.bundle, mutations={'score':truncate})).run([DOC])
        self.assertEqual(out['documents'][0]['units'][0]['status'], 'complete')
        dimension = out['documents'][0]['units'][0]['dimensions'][1]
        self.assertEqual(dimension['complete_sentence_refs'], [REF])
        self.assertEqual(dimension['score_basis']['refs'][0]['quote'], TEXT[:9])

    def test_basis_disagreement_rejected_without_publishing_conflict(self):
        def mismatch(d, p):
            d['dimensions'][3]['score_basis']['evaluated_score'] = 5
            return d
        out = Pipeline(self.bundle, FakeProvider(self.bundle, mutations={'score':mismatch})).run([DOC])
        d = out['documents'][0]['units'][0]['dimensions'][3]
        self.assertIsNone(d['score'])
        self.assertIsNone(d['score_basis']['evaluated_score'])
        self.assertEqual(d['score_basis']['score_alignment'], 'unavailable')
        self.assertEqual(out['documents'][0]['units'][0]['status'], 'partial')
        self.assertEqual(len(out['documents'][0]['units'][0]['dimensions']),5)

    def test_children_finish_before_parent_and_no_average(self):
        fake = FakeProvider(self.bundle, [unit('parent','whole'), unit('child','small','parent')], {'parent':'reading_whole','child':'reading_single'})
        out = Pipeline(self.bundle,fake).run([DOC])
        ev = next(p for n,p in fake.history if n == 'dimension' and p['unit']['id'] == 'parent')
        self.assertEqual(ev['children'][0]['route'], 'reading_single')
        self.assertIn('dimensions', out['documents'][0]['units'][0])
        self.assertEqual(len(out['documents'][0]['units'][0]['dimensions']), 7)

    def test_failed_unit_does_not_stop_next(self):
        def broken(d,p):
            d['route'] = 'invented'
            return d
        fake = FakeProvider(self.bundle,[unit('bad'),unit('good')],mutations={('route','bad'):broken})
        out = Pipeline(self.bundle,fake).run([DOC])
        self.assertEqual([u['status'] for u in out['documents'][0]['units']], ['unavailable','complete'])
        self.assertEqual(sum(n == 'route' and p['unit']['id'] == 'bad' for n,p in fake.history), 2)

    def test_structural_repair_is_bounded_and_can_recover(self):
        counter = [0]
        def broken_once(d,p):
            counter[0] += 1
            if counter[0] == 1: d['coverage'] = []
            return d
        fake = FakeProvider(self.bundle, mutations={'structure':broken_once})
        out = Pipeline(self.bundle,fake).run([DOC])
        self.assertEqual(out['documents'][0]['units'][0]['status'], 'complete')
        self.assertIsNotNone(fake.history[1][1]['repair'])

    def test_fake_reference_rejected(self):
        with self.assertRaises(ContractError): check_refs({**REF, 'quote':'invented'}, {'b1':TEXT})
        with self.assertRaises(ContractError): check_refs({**REF, 'end':999}, {'b1':TEXT})

    def test_format_only_quote_alignment_preserves_source(self):
        source = '他说“hello”，然后继续。'
        ref = {'block_id':'b','start':0,'end':3,'quote':'他说"hello"'}
        self.assertTrue(align_unique_quotes(ref, {'b':source}))
        check_refs(ref, {'b':source})
        self.assertEqual(ref['quote'], '他说“hello”')
        bad = {'block_id':'b','start':0,'end':3,'quote':'他说“goodbye”'}
        self.assertFalse(align_unique_quotes(bad, {'b':source}))
        with self.assertRaises(ContractError): check_refs(bad, {'b':source})

    def test_extra_closing_quote_only_removed_for_unique_exact_source(self):
        source='He said, “The program succeeded because it helped students connect. More followed.”'
        quote='“The program succeeded because it helped students connect.”'
        ref={'block_id':'b','start':0,'end':len(quote),'quote':quote}
        changes=align_unique_quotes(ref,{'b':source})
        self.assertEqual(len(changes),1)
        self.assertEqual(ref['quote'],'“The program succeeded because it helped students connect.')
        check_refs(ref,{'b':source})

    def test_coverage_gap_rejected(self):
        with self.assertRaises(ContractError):
            check_structure({'units':[unit()], 'coverage':[], 'issues':[]}, {'b1':TEXT},100)

    def test_no_key_no_network_no_model_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch('urllib.request.urlopen') as network:
            out = Pipeline(self.bundle,ArkMini(self.bundle.config['provider'])).run([DOC,DOC])
            self.assertEqual(out['summary']['network_calls'],0)
            self.assertEqual(out['summary']['document_failures'],2)
            network.assert_not_called()

    def test_remote_disconnect_uses_bounded_retry_instead_of_crashing_batch(self):
        import http.client
        with patch.dict(os.environ,{'ARK_API_KEY':'synthetic-not-a-real-key'}), patch('urllib.request.urlopen',side_effect=http.client.RemoteDisconnected('synthetic')) as network:
            out=Pipeline(self.bundle,ArkMini(self.bundle.config['provider'])).run([DOC])
            self.assertEqual(out['summary']['document_failures'],1)
            self.assertEqual(network.call_count,self.bundle.config['limits']['attempts_per_node'])

    def test_audit_schema_requires_zero_count_for_empty_list(self):
        schema=self.bundle.schemas['judgment_metrics']
        valid={'rule_application':{'anchor_score':None,'anchor_quote':None,'criterion_evidence':'synthetic','adjacent_band_check':'synthetic'},'judgment_items':[],'judgment_count':0}
        validate(valid,schema)
        del valid['judgment_count']
        with self.assertRaises(ContractError):validate(valid,schema)

    def test_total_request_budget(self):
        self.bundle.config['limits']['max_calls_per_run'] = 3
        fake = FakeProvider(self.bundle)
        out = Pipeline(self.bundle,fake).run([DOC,DOC])
        self.assertEqual(fake.calls,3)
        self.assertEqual(out['documents'][0]['units'][0]['status'],'partial')

    def test_no_dictionary_contextual_judgment_allows_cloze(self):
        fake = FakeProvider(self.bundle,routes={'u1':'cloze_single'})
        out = Pipeline(self.bundle,fake).run([DOC])
        result = out['documents'][0]['units'][0]
        self.assertEqual(result['status'],'complete')
        self.assertNotIn('d1', result['missing_dimensions'])
        self.assertIsNotNone(result['dimensions'][0]['score'])

    def test_missing_contextual_judgment_stays_partial(self):
        def omit(data,payload):
            data['sense_matches']=[]
            return data
        fake=FakeProvider(self.bundle,routes={'u1':'cloze_single'},mutations={'evidence':omit})
        result=Pipeline(self.bundle,fake).run([DOC])['documents'][0]['units'][0]
        self.assertEqual(result['status'],'partial')
        self.assertIn('d1',result['missing_dimensions'])

    def test_unlisted_word_and_same_word_different_senses(self):
        self.assertNotIn('book',self.bundle.resources['rare_examples']['lemmas'])
        for lemma in ('book','address'):
            lookup=self.bundle.lookup({'occurrences':[{'id':'w','lemma':lemma,'pos':'verb','exemption':None,'ref':REF}],'complete':True,'coverage_note':''})
            for judgment,expected in [('rare',True),('common',False),('uncertain','unknown')]:
                match={'occurrence_id':'w','sense_id':None,'basis':'contextual_model','example_lemma':None,'common_sense':'usual','contextual_sense':'current','judgment':judgment,'reason':'context comparison','ref':REF}
                result=self.bundle.senses(lookup,[match])[0]
                self.assertEqual(result['rare_sense'],expected)
                self.assertEqual(result['rule_basis'],'contextual_comparison_not_dictionary_rank')
                if lemma == 'book':
                    match['example_lemma']='book'
                    corrected=self.bundle.senses(lookup,[match])[0]
                    self.assertIsNone(corrected['example_source'])
                    self.assertTrue(corrected['source_warnings'])
                    self.assertEqual(corrected['rare_sense'],expected)

    def test_fabricated_dictionary_sense_rejected(self):
        lookup = self.bundle.lookup({'occurrences':[{'id':'w','lemma':'the','pos':'article','exemption':None,'ref':REF}], 'complete':True,'coverage_note':''})
        with self.assertRaises(ContractError):
            self.bundle.senses(lookup,[{'occurrence_id':'w','sense_id':'fake','ref':REF,'reason':'fake'}])

    def test_listening_unsupported_does_not_wait(self):
        fake = FakeProvider(self.bundle,routes={'u1':None})
        out = Pipeline(self.bundle,fake).run([DOC])
        self.assertEqual(out['documents'][0]['units'][0]['status'],'unsupported')
        self.assertEqual(fake.calls,2)

    def test_invalid_document_continues_batch(self):
        fake = FakeProvider(self.bundle)
        out = Pipeline(self.bundle,fake).run([{'id':'bad'},DOC])
        self.assertEqual(out['documents'][0]['status'],'unavailable')
        self.assertEqual(out['documents'][1]['units'][0]['status'],'complete')

    def test_jsonl_bad_line_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'input.jsonl'
            p.write_text('broken\n'+json.dumps(DOC),encoding='utf-8')
            docs = load_input(p)
            self.assertEqual(docs[0]['source_line'],'broken')
            self.assertEqual(docs[1],DOC)

    def test_schema_rejects_boolean_score(self):
        schema = self.bundle.schemas['score']['properties']['dimensions']['items']['properties']['score']
        with self.assertRaises(ContractError): validate(True,schema)

    def test_unique_quote_offset_repair_and_ambiguity(self):
        ref = {'block_id':'b', 'start':0, 'end':3, 'quote':'world'}
        self.assertEqual(len(align_unique_quotes(ref,{'b':'hello world'})),1)
        self.assertEqual((ref['start'],ref['end']),(6,11))
        check_refs(ref,{'b':'hello world'})
        ambiguous = {'block_id':'b','start':1,'end':2,'quote':'go'}
        self.assertEqual(align_unique_quotes(ambiguous,{'b':'go go'}),[])
        with self.assertRaises(ContractError): check_refs(ambiguous,{'b':'go go'})

    def test_no_other_model_can_be_loaded(self):
        from english_difficulty import resources
        original = resources.read_json
        def changed(path):
            data = original(path)
            if str(path).endswith('pipeline.json'):
                data['provider']['model'] = 'other-model'
            return data
        with patch.object(resources,'read_json',changed), self.assertRaises(ContractError):
            Bundle(ROOT)

    def test_grammar_distractors_require_each_option_evidence(self):
        rule={'projection':'grammar_distractors'}
        score, reason = project_dimension(rule,{'metrics':{},'missing':[]},self.bundle.config['scoring'])
        self.assertIsNone(score)
        options=[{'is_correct':i==0,'temporarily_valid':False,'requires_rule_elimination':True,'refs':[REF],'reason':'synthetic'} for i in range(4)]
        self.assertEqual(project_dimension(rule,{'metrics':{'option_evaluations':options},'missing':[]},self.bundle.config['scoring'])[0],1)

    def test_all_routes_reach_terminal_state(self):
        for rid,rubric in self.bundle.rubrics.items():
            with self.subTest(route=rid):
                fake = FakeProvider(self.bundle,[unit(grain=rubric['grain'])],{'u1':rid})
                out = Pipeline(self.bundle,fake).run([DOC])
                self.assertEqual(out['status'],'finished')
                self.assertIn(out['documents'][0]['units'][0]['status'],('complete','partial','unavailable'))

    def test_every_active_node_schema_compiles_to_strict_ark_shape(self):
        class StrictFixture(FakeProvider):
            def complete(self, system, payload, schema, node):
                strict=to_strict_schema(schema)
                self_outer.assertEqual(strict['additionalProperties'],False)
                return super().complete(system,payload,schema,node)
        self_outer=self
        for rid,rubric in self.bundle.rubrics.items():
            with self.subTest(route=rid):
                fake=StrictFixture(self.bundle,[unit(grain=rubric['grain'])],{'u1':rid})
                Pipeline(self.bundle,fake).run([DOC])
                seen={payload['rubric']['dimensions'][0]['id'] for node,payload in fake.history if node=='dimension'}
                self.assertEqual(seen,{dim['id'] for dim in rubric['dimensions']})

    def test_provider_sends_server_side_strict_schema_not_prompt_only(self):
        captured=[]
        answer={'choices':[{'finish_reason':'stop','message':{'content':'{"route":null,"reason":"synthetic","refs":[]}'}}]}
        def urlopen(req,timeout):
            captured.append(json.loads(req.data.decode('utf-8')))
            return io.BytesIO(json.dumps(answer).encode('utf-8'))
        with patch.dict(os.environ,{'ARK_API_KEY':'synthetic-not-a-real-key'}), patch('urllib.request.urlopen',side_effect=urlopen):
            ArkMini(self.bundle.config['provider']).complete('synthetic prompt',{},self.bundle.schemas['route'],'route')
        body=captured[0]
        self.assertEqual(body['model'],'doubao-seed-2-0-mini-260215')
        self.assertEqual(body['response_format']['type'],'json_schema')
        self.assertIs(body['response_format']['json_schema']['strict'],True)
        self.assertEqual(body['messages'][0]['content'],'synthetic prompt')
        self.assertEqual(body['response_format']['json_schema']['schema']['additionalProperties'],False)

    def test_cloze_counts_derived_from_children(self):
        units = [unit('p','whole')] + [unit(f'c{i}','small','p') for i in range(15)]
        routes = {'p':'cloze_whole', **{f'c{i}':None for i in range(15)}}
        def evidence(data,payload):
            target = data['dimensions'][2]
            target['metrics'] = {'total_items':999, 'clue_items':[{'item_id':f'c{i}', 'scope':'same_sentence' if i < 9 else 'cross_paragraph', 'refs':[REF]} for i in range(15)]}
            return data
        fake = FakeProvider(self.bundle,units,routes,{('evidence','p'):evidence})
        result = Pipeline(self.bundle,fake).run([DOC])['documents'][0]['units'][0]
        self.assertEqual(result['dimensions'][2]['score'],4)
        self.assertEqual(result['evidence']['dimensions'][2]['metrics']['total_items'],15)

if __name__ == '__main__':
    unittest.main()
