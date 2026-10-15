"""Deterministic projections after semantic evidence extraction."""
from decimal import Decimal, ROUND_HALF_UP
from .contracts import require

def token_selectors(unit_refs):
    """Assign stable IDs to literal English token occurrences, without classification."""
    found = {}
    for ref in unit_refs:
        text = ref['quote']
        start = None
        for index in range(len(text) + 1):
            char = text[index] if index < len(text) else ''
            alpha = bool(char) and char.isascii() and char.isalpha()
            connector = char in ("'", '-', '’') and start is not None and index + 1 < len(text) and text[index + 1].isascii() and text[index + 1].isalpha()
            if alpha and start is None:
                start = index
            if start is not None and not (alpha or connector):
                absolute = ref['start'] + start
                key = (ref['block_id'], absolute, ref['start'] + index)
                found[key] = {'block_id':ref['block_id'],'start':absolute,'end':ref['start']+index,'quote':text[start:index]}
                start = None
    return [{'id': f't{i+1}', 'ref': ref} for i, ref in enumerate(found.values())]

def word_count(text):
    """Count alphabetic tokens; internal apostrophes/hyphens remain in one word.

    This is a documented counting convention, never a semantic classifier.
    Numbers and underscore blank markers are not English words.
    """
    count, inside = 0, False
    for index, char in enumerate(text):
        alpha = char.isascii() and char.isalpha()
        connector = char in "'-’" and inside and index + 1 < len(text) and text[index + 1].isascii() and text[index + 1].isalpha()
        if alpha and not inside:
            count += 1
        inside = alpha or connector
    return count

def project_dimension(dimension, evidence, cfg):
    kind = dimension.get('projection')
    if evidence['missing']:
        return None, '缺少必要证据：' + '；'.join(evidence['missing'])
    metrics = evidence['metrics']
    if kind == 'grammar_distractors':
        options = metrics.get('option_evaluations')
        if not isinstance(options, list) or len(options) != 4:
            return None, '缺少四个选项逐项有效性证据，不能按错误选项数量代替有效干扰数'
        if not all(isinstance(o,dict) and all(type(o.get(k)) is bool for k in ('is_correct','temporarily_valid','requires_rule_elimination')) and o.get('refs') and o.get('reason') for o in options):
            return None, '选项有效性证据合同不完整'
        if sum(o['is_correct'] for o in options) != 1:
            return None, '正确项覆盖不唯一'
        count = sum(not o['is_correct'] and o['temporarily_valid'] and o['requires_rule_elimination'] for o in options)
        if count < 3:
            return count+1, f'模型逐项判断后，满足全部条件的有效干扰项为{count}个'
        distinction = metrics.get('distinction')
        if distinction == 'same_grammar_forms':
            return 4, '3个有效干扰项，模型判断为同语法点不同形式'
        if distinction == 'fine_rules':
            return 5, '3个有效干扰项，模型判断须细分语法规则'
        return None, '缺少三个有效干扰项的辨析性质证据'
    if kind == 'seven_length':
        refs = metrics.get('passage_refs', [])
        if not refs:
            return None, '缺少文章正文引用'
        spans = {}
        for ref in refs:
            spans.setdefault(ref['block_id'], []).append((ref['start'], ref['end']))
        for ranges in spans.values():
            ordered = sorted(ranges)
            require(all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:])), 'overlapping passage refs')
        words = sum(word_count(r['quote']) for r in refs)
        score = 1 + sum(words > bound for bound in cfg['seven_length_upper_bounds'])
        return score, f'正文按配置的词数规则计为{words}词，五档描述投影为{score}分'
    if kind == 'cloze_span':
        keys = ['total_items', 'same_sentence_items', 'adjacent_sentence_items', 'cross_paragraph_items', 'global_items']
        if not all(type(metrics.get(k)) is int and metrics[k] >= 0 for k in keys):
            return None, '缺少完整、互斥的逐题线索范围计数'
        total, same, adjacent, cross, glob = (metrics[k] for k in keys)
        if total == 0 or same + adjacent + cross + glob != total:
            return None, '逐题范围计数不覆盖全部题目'
        same_pct = Decimal(same) * 100 / total
        local_pct = Decimal(same + adjacent) * 100 / total
        limits = cfg['cloze_span']
        if same_pct >= limits['same_sentence_one_min']:
            score = 1
        elif limits['same_sentence_two_min'] <= same_pct <= limits['same_sentence_two_max'] and cross == glob == 0:
            score = 2
        elif limits['local_three_min'] <= local_pct <= limits['local_three_max']:
            score = 3
        elif limits['local_four_min'] <= local_pct <= limits['local_four_max']:
            score = 4
        elif local_pct < limits['local_five_below']:
            score = 5
        else:
            return None, f'五档原描述未明确覆盖此组合：当句{same_pct}%，当句及邻句{local_pct}%；不使用被弃用的D列补档'
        return score, f'当句{same}/{total}，当句及邻句{same+adjacent}/{total}；按五档描述计{score}分'
    raise ValueError(f'Unknown projection: {kind}')

def aggregate(dimensions, rubric, config):
    by_id = {d['id']: d for d in dimensions}
    weighted = Decimal(0)
    available = Decimal(0)
    missing = []
    for rule in rubric['dimensions']:
        score = by_id[rule['id']]['score']
        if score is None:
            missing.append(rule['id'])
            continue
        require(type(score) is int and 1 <= score <= 5, 'invalid dimension score')
        weight = Decimal(str(rule['weight']))
        weighted += weight * score
        available += weight
    if not available:
        return {'status': 'unavailable', 'score': None, 'stars': None, 'three_band': None, 'available_weight': 0, 'missing_dimensions': missing}
    score = weighted / available
    stars = sum(score >= Decimal(str(bound)) for bound in config['star_lower_bounds'])
    three = config['three_band']
    band = '易' if score < Decimal(str(three['easy_below'])) else '难' if score > Decimal(str(three['hard_above'])) else '中'
    return {'status': 'partial' if missing else 'complete', 'score': str(score),
            'display_score': str(score.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
            'stars': stars, 'three_band': band, 'available_weight': float(available),
            'missing_dimensions': missing}
