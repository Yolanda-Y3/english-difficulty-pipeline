"""Compile local contracts to Ark's strict structured-output subset.

Local bounds and citation checks remain authoritative after generation.
Unsupported range keywords are intentionally omitted from the wire schema.
"""

from .contracts import require


def to_strict_schema(schema):
    def convert(node):
        require(isinstance(node, dict), 'invalid schema node')
        kind = node.get('type')
        if isinstance(kind, list):
            require(len(kind) >= 2, 'invalid union schema')
            require(set(node) <= {'type', 'description', 'minimum', 'maximum'}, 'unsupported union constraints')
            description = {'description': node['description']} if 'description' in node else {}
            return {'anyOf': [{'type': item} for item in kind], **description}
        result = {}
        for key in ('type', 'description', 'enum'):
            if key in node:
                result[key] = node[key]
        if kind == 'object':
            props = node.get('properties')
            require(isinstance(props, dict), 'strict object needs properties')
            require(set(node.get('required', [])) == set(props), 'every strict object property must be required')
            require(node.get('additionalProperties') is False, 'strict object must reject extra fields')
            result['properties'] = {key: convert(value) for key, value in props.items()}
            result['required'] = list(props)
            result['additionalProperties'] = False
        elif kind == 'array':
            require('items' in node, 'strict array needs items')
            result['items'] = convert(node['items'])
        return result
    return convert(schema)
