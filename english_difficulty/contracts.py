"""Structural contract and source-reference checks. No semantic heuristics."""

class ContractError(ValueError):
    pass

def require(condition, message):
    if not condition:
        raise ContractError(message)

def validate(value, schema, path='$'):
    # The shipped contracts deliberately use this small, audited JSON Schema subset.
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool, 'null': type(None)}
    if 'type' in schema:
        wanted = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        require(any(type(value) is types[t] for t in wanted), f'{path}: invalid type')
    if 'enum' in schema:
        require(value in schema['enum'], f'{path}: invalid enum')
    if type(value) is dict:
        absent = [k for k in schema.get('required', []) if k not in value]
        require(not absent, f'{path}: missing fields {absent}')
        props = schema.get('properties', {})
        if schema.get('additionalProperties') is False:
            unexpected = sorted(set(value) - set(props))
            require(not unexpected, f'{path}: unexpected fields {unexpected}; allowed keys {sorted(props)}')
        for key, spec in props.items():
            if key in value:
                validate(value[key], spec, f'{path}.{key}')
    if type(value) is list and 'items' in schema:
        require(len(value) >= schema.get('minItems',0), f'{path}: too few items')
        require(len(value) <= schema.get('maxItems',len(value)), f'{path}: too many items')
        for i, item in enumerate(value):
            validate(item, schema['items'], f'{path}[{i}]')
    if type(value) is int:
        require(value >= schema.get('minimum', value), f'{path}: below minimum')
        require(value <= schema.get('maximum', value), f'{path}: above maximum')

def unique(items, name):
    ids = [x['id'] for x in items]
    require(all(ids) and len(ids) == len(set(ids)), f'{name}: empty/duplicate ids')

def check_refs(value, blocks):
    if isinstance(value, dict):
        if {'block_id', 'start', 'end', 'quote'} <= value.keys():
            key, start, end = value['block_id'], value['start'], value['end']
            require(type(key) is str and type(start) is int and type(end) is int, 'invalid reference types')
            require(key in blocks, 'unknown reference block')
            require(0 <= start < end <= len(blocks[key]), 'reference outside source')
            require(blocks[key][start:end] == value['quote'], f"quote does not match source at {key}[{start}:{end}]; original slice is {blocks[key][start:end]!r}; copy original characters or choose an exact original quote")
        for child in value.values():
            check_refs(child, blocks)
    elif isinstance(value, list):
        for child in value:
            check_refs(child, blocks)

def align_unique_quotes(value, blocks):
    """Repair only offsets of an exact, uniquely occurring quote, never its text.

    No semantic selection: ambiguous/reworded quotes still fail validation.
    Coverage selectors sharing the identical old tuple follow the same repair.
    """
    changes = []
    offsets = {}
    def normalized(text):
        mapping = {'“':'"','”':'"','‘':"'",'’':"'",'‑':'-','–':'-','．':'.'}
        chars, positions = [], []
        for i,c in enumerate(text):
            c = ' ' if c.isspace() else mapping.get(c,c)
            if c == ' ' and chars and chars[-1] == '.':
                continue
            if c == ' ' and chars and chars[-1] == ' ':
                continue
            chars.append(c); positions.append(i)
        return ''.join(chars), positions
    def visit(v):
        if isinstance(v, dict):
            if {'block_id', 'start', 'end', 'quote'} <= v.keys():
                key, quote = v['block_id'], v['quote']
                if type(key) is str and key in blocks and type(quote) is str and quote:
                    source = blocks[key]
                    start, end = v['start'], v['end']
                    if type(start) is int and type(end) is int and source[max(0,start):max(0,end)] != quote:
                        at = source.find(quote)
                        if at >= 0 and source.find(quote, at + 1) < 0:
                            offsets[(key,start,end)] = (at,at+len(quote))
                            changes.append({'block_id':key,'old':[start,end],'new':[at,at+len(quote)]})
                            v['start'], v['end'] = at, at+len(quote)
                        elif at < 0:
                            # A model sometimes closes a quotation after a
                            # sentence whose source quotation continues. Only
                            # strip this one ornamental glyph when the rest is
                            # an exact, unique source substring.
                            without_close = quote[:-1] if quote.endswith('”') else ''
                            close_at = source.find(without_close) if without_close else -1
                            if close_at >= 0 and source.find(without_close, close_at + 1) < 0:
                                left, right = close_at, close_at + len(without_close)
                                offsets[(key,start,end)] = (left,right)
                                changes.append({'block_id':key,'old':[start,end],'new':[left,right],
                                                'format_only_quote_alignment':True,'original_model_quote':quote})
                                v.update(start=left,end=right,quote=source[left:right])
                            else:
                                norm_source, positions = normalized(source)
                                norm_quote, _ = normalized(quote)
                                at = norm_source.find(norm_quote)
                                if norm_quote and at >= 0 and norm_source.find(norm_quote, at+1) < 0:
                                    left, right = positions[at], positions[at+len(norm_quote)-1]+1
                                    offsets[(key,start,end)] = (left,right)
                                    changes.append({'block_id':key,'old':[start,end],'new':[left,right],'format_only_quote_alignment':True,'original_model_quote':quote})
                                    v.update(start=left,end=right,quote=source[left:right])
            for child in v.values(): visit(child)
        elif isinstance(v, list):
            for child in v: visit(child)
    visit(value)
    if isinstance(value, dict):
        for selector in value.get('coverage', []):
            key = (selector['block_id'], selector['start'], selector['end'])
            if key in offsets:
                selector['start'],selector['end'] = offsets[key]
    return changes

def check_structure(data, blocks, max_units):
    unique(data['units'], 'units')
    require(len(data['units']) <= max_units, 'too many units')
    require(data['units'] or data['issues'], 'empty structure without explanation')
    units = {u['id']: u for u in data['units']}
    for u in units.values():
        require(u['refs'], 'unit needs source references')
        if u['parent_id'] is not None:
            require(u['grain'] == 'small' and u['parent_id'] in units and units[u['parent_id']]['grain'] == 'whole', 'invalid parent')
    for c in data['coverage']:
        require(c['block_id'] in blocks and 0 <= c['start'] < c['end'] <= len(blocks[c['block_id']]), 'invalid coverage span')
        require(c['unit_id'] is None or c['unit_id'] in units, 'invalid coverage unit')
        if c['unit_id'] is not None:
            refs = units[c['unit_id']]['refs']
            require(any(r['block_id'] == c['block_id'] and r['start'] <= c['start'] and r['end'] >= c['end'] for r in refs), 'coverage not referenced by unit')
    for key, text in blocks.items():
        spans = sorted((c['start'], c['end']) for c in data['coverage'] if c['block_id'] == key)
        cursor = 0
        for start, end in spans:
            require(start <= cursor, 'uncovered source gap')
            cursor = max(cursor, end)
        require(cursor == len(text), 'uncovered source tail')

def check_dimensions(data, rubric):
    unique(data['dimensions'], 'dimensions')
    expected={d['id'] for d in rubric['dimensions']}
    actual={d['id'] for d in data['dimensions']}
    require(actual == expected, f'dimension coverage mismatch: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}; required={sorted(expected)}')
