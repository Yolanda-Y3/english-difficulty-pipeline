import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .resources import Bundle, read_json
from .provider import ArkMini
from .pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]

def load_input(path):
    if path.suffix.lower() == '.txt':
        return [{'id': path.stem, 'blocks': [{'id': 'b1', 'text': path.read_text(encoding='utf-8-sig')}]}]
    if path.suffix.lower() == '.jsonl':
        docs = []
        for line_no, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
            if not line.strip():
                continue
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError:
                docs.append({'id': f'line-{line_no}', 'parse_error': 'invalid JSON', 'source_line': line})
        return docs
    if path.suffix.lower() == '.json':
        data = read_json(path)
        return data if isinstance(data, list) else [data]
    raise ValueError('输入支持UTF-8 TXT、JSON、JSONL；PDF/DOCX请从已有capture导出blocks，不在此处重新拆解原件')

def report_markdown(result):
    rows = ['# 英语难度计算结果', '', '本批已结束，无中途人工等待。complete/partial/unavailable描述计算完整性，不代表教师审核或生产验收。', '',
            f"模型：{result['versions']['model']}", '', '## 汇总', '', '```json', json.dumps(result['summary'], ensure_ascii=False, indent=2), '```', '']
    for doc in result['documents']:
        rows += [f"## 输入：{doc['id']}", '']
        if doc.get('reason'):
            rows += [doc['reason'], '']
        for unit in doc['units']:
            rows += [f"### {unit['unit_id']} · {unit['status']}", '',
                     f"题型：{unit.get('route', '未确定')}；分数：{unit.get('display_score', '—')}；星级：{unit.get('stars', '—')}；有效权重：{unit.get('available_weight', '—')}", '']
            if unit.get('reason'):
                rows += [unit['reason'], '']
            for dim in unit.get('dimensions', []):
                rows += [f"- {dim['id']}：{dim['score'] if dim['score'] is not None else '不可计算'}；{dim.get('projection_reason', dim['reason'])}"]
                if dim.get('score_basis'):
                    b = dim['score_basis']
                    rows += [f"  - 当前分值依据（{b['evaluated_score']}）：{b['student_demand']}",
                             f"  - 档位对应：{b['anchor_match']}", f"  - 采用理由：{b['why_this_score']}"]
                    rows += [f"  - 完整相关句：{r['quote']}" for r in dim.get('complete_sentence_refs',b['refs'])]
            rows.append('')
    return '\n'.join(rows)

def main():
    parser = argparse.ArgumentParser(description='英语难度链路：默认不调用模型，所有节点仅使用豆包2.0mini')
    parser.add_argument('command', choices=['check', 'run'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--live', action='store_true', help='调用已配置的豆包2.0mini；仅run适用')
    parser.add_argument('--max-calls', type=int, default=None, help='整批请求上限，包含自动重试，不超过配置上限')
    args = parser.parse_args()
    bundle = Bundle(args.root)
    if args.command == 'check':
        print(json.dumps({'config': 'valid', 'versions': bundle.snapshot(), 'routes': len(bundle.routes),
                          'dimensions': sum(len(r['dimensions']) for r in bundle.rubrics.values()),
                          'vocabulary_rows': len(bundle.resources['vocabulary']['entries']),
                          'api_key_present': bool(os.environ.get(bundle.config['provider']['api_key_env'])),
                          'dictionary_configured': bundle.resources['dictionary']['edition'] is not None}, ensure_ascii=False, indent=2))
        return
    if args.input is None:
        parser.error('run需要--input')
    if not args.live:
        parser.error('真实模型运行需显式--live；离线核查使用check和unittest')
    if args.max_calls is not None:
        if not 0 < args.max_calls <= bundle.config['limits']['max_calls_per_run']:
            parser.error('--max-calls超出允许范围')
        bundle.config['limits']['max_calls_per_run'] = args.max_calls
    documents = load_input(args.input)
    output = args.output or args.root / 'runs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    # Snapshot actual configuration/prompts/resources used. No generated content hashes.
    snapshot = {'config': bundle.config, 'routes': bundle.routes, 'rubrics': bundle.rubrics,
                'prompts': bundle.prompts, 'schemas': bundle.schemas, 'resources': bundle.resources}
    (output / 'configuration_snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    result = Pipeline(bundle, ArkMini(bundle.config['provider'])).run(documents)
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'report.md').write_text(report_markdown(result), encoding='utf-8')
    print(json.dumps({'output': str(output.resolve()), **result['summary']}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
