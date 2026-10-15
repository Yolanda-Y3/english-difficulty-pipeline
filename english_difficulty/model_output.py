"""Lossless container repair and numeric mapping of a model's semantic units.

This module never decides whether a structure exists or should be counted.
Those judgments remain in the dimension model's cited item list.
"""

from .contracts import require


def normalize_dimension_container(value, route, lexical_skip, counted=False, projection=None):
    """Repair only misplaced/omitted empty containers, never missing evidence."""
    repairs = []
    if not isinstance(value, dict):
        return repairs
    dimensions = value.get('dimensions')
    if not isinstance(dimensions, list) or len(dimensions) != 1 or not isinstance(dimensions[0], dict):
        return repairs
    dim = dimensions[0]
    for key in ('sense_matches', 'notes'):
        if key not in value and key in dim:
            value[key] = dim.pop(key)
            repairs.append(f'moved {key} to response')
    if 'notes' not in value:
        value['notes'] = []
        repairs.append('added empty notes')
    if lexical_skip and 'sense_matches' not in value:
        value['sense_matches'] = []
        repairs.append('added empty sense_matches for lexical-free route')
    for key in ('complete_sentence_refs', 'scope_reason', 'score', 'reason', 'refs', 'evidence_ids', 'score_basis'):
        if key in value and key not in dim:
            dim[key] = value.pop(key)
            repairs.append(f'moved {key} to dimension')
    metrics = dim.get('metrics')
    if isinstance(metrics, dict):
        for key in ('rule_application', 'judgment_items', 'judgment_count', 'option_evaluations', 'distinction'):
            if key in dim and key not in metrics:
                metrics[key] = dim.pop(key)
                repairs.append(f'moved {key} to metrics')
        if route != 'grammar_choice' or dim.get('id') != 'd4':
            # Empty grammar-only defaults carry no semantic judgment.
            for key, empty in (('option_evaluations', []), ('distinction', None)):
                if key in metrics and metrics[key] == empty:
                    metrics.pop(key)
                    repairs.append(f'removed empty {key} outside grammar distractors')
        if not counted and isinstance(metrics.get('judgment_items'), list) and metrics['judgment_items']:
            # This list is a counting instrument. On a non-counted dimension it
            # is not part of the score; raw output remains in the provider log.
            repairs.append(f'removed {len(metrics["judgment_items"])} irrelevant counting items')
            metrics['judgment_items'] = []
            metrics['judgment_count'] = 0
        if projection == 'cloze_span' and isinstance(metrics.get('clue_items'), list):
            for key in ('total_items', 'same_sentence_items', 'adjacent_sentence_items',
                        'cross_paragraph_items', 'global_items'):
                if key in metrics:
                    metrics.pop(key)
                    repairs.append(f'removed derived {key}; program recomputes it')
    for key, empty in (('option_evaluations', []), ('distinction', None)):
        if (route != 'grammar_choice' or dim.get('id') != 'd4') and key in dim and dim[key] == empty:
            dim.pop(key)
            repairs.append(f'removed empty dimension-level {key}')
    return repairs


def project_counted_syntax(dim, rule, count_config):
    """Map the model's cited structure count through existing rubric bands.

    Zero complex structures still require the model's simple-sentence and
    sentence-length judgment, so the program deliberately leaves 1/2 alone.
    """
    if dim['missing'] or not count_config or count_config.get('dimension') != dim['id']:
        return None
    metrics = dim['metrics']
    items = metrics['judgment_items']
    require(all(type(item['counted']) is bool for item in items), 'counted items need boolean decisions')
    count = sum(item['counted'] for item in items)
    if metrics['judgment_count'] != count:
        metrics['model_suggested_count'] = metrics['judgment_count']
        metrics['judgment_count'] = count
    if count == 0:
        require(dim['score'] in (1, 2), 'zero complex structures cannot use a complex-structure band')
        return None
    band = next((band for band in count_config['bands']
                 if count >= band['min'] and (band['max'] is None or count <= band['max'])), None)
    require(band is not None, 'structure count has no original-rubric band')
    score = band['score']
    if dim['score'] == score:
        return None
    old_score = dim['score']
    metrics['model_suggested_score'] = old_score
    metrics['model_original_reason'] = dim['reason']
    note = f'逐项证据清单计入{count}个复杂语法结构；依本题型原表对应{score}分。'
    dim['score'] = score
    dim['reason'] = note
    basis = dim['score_basis']
    basis['evaluated_score'] = score
    basis['anchor_match'] = rule['anchors'][score - 1]
    basis['why_this_score'] = note
    audit = metrics['rule_application']
    audit['anchor_score'] = score
    audit['anchor_quote'] = rule['anchors'][score - 1]
    audit['criterion_evidence'] = note
    audit['adjacent_band_check'] = f'计数为{count}，按原表数量边界归入{score}分档。'
    return {'model_suggested_score': old_score, 'projected_score': score, 'count': count}


def apply_existing_projection(dim, rule, score, explanation):
    """Keep the model's semantic evidence; derive redundant numeric fields."""
    if score is None or dim['score'] == score:
        return
    metrics = dim['metrics']
    metrics['model_suggested_score'] = dim['score']
    metrics['model_original_reason'] = dim['reason']
    dim['score'] = score
    dim['reason'] = explanation
    basis = dim['score_basis']
    basis['evaluated_score'] = score
    basis['anchor_match'] = rule['anchors'][score - 1]
    basis['why_this_score'] = explanation
    audit = metrics['rule_application']
    audit['anchor_score'] = score
    audit['anchor_quote'] = rule['anchors'][score - 1]
    audit['criterion_evidence'] = explanation
    audit['adjacent_band_check'] = f'原表投影结果为{score}分，以本节点逐项证据为输入。'


def align_original_anchor(dim, rule):
    """Copy the rubric anchor literally; it is not a model judgment."""
    audit = dim['metrics']['rule_application']
    score = dim['score']
    expected = rule['anchors'][score - 1] if score is not None else None
    if audit['anchor_quote'] != expected:
        dim['metrics']['model_suggested_anchor_quote'] = audit['anchor_quote']
        audit['anchor_quote'] = expected
    if audit['anchor_score'] != score:
        dim['metrics']['model_suggested_anchor_score'] = audit['anchor_score']
        audit['anchor_score'] = score
