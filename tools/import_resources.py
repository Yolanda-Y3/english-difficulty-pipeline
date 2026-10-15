"""Read-only source import. Requires openpyxl/pdfplumber only when rebuilding resources."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES = [
    ("grammar_choice", "small"), ("grammar_fill_single", "small"),
    ("grammar_fill_whole", "whole"), ("cloze_single", "small"),
    ("cloze_whole", "whole"), ("reading_single", "small"),
    ("reading_whole", "whole"), ("seven_single", "small"),
    ("seven_whole", "whole"), ("application_writing", "writing"),
    ("continuation_writing", "writing"),
]

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    args = parser.parse_args()
    import openpyxl
    import pdfplumber
    book_path = args.source_dir / "10.12高考英语全题型难度星级分层标准.xlsx"
    book = openpyxl.load_workbook(book_path, data_only=True)
    routes = []
    for index, (route, grain) in enumerate(ROUTES, 1):
        sheet = book.worksheets[index]
        dimensions = []
        for row in range(4, sheet.max_row + 1):
            if type(sheet.cell(row, 1).value) is not int:
                continue
            weight = sheet.cell(row, 3).value
            weight = float(weight.strip('%')) / 100 if isinstance(weight, str) else float(weight)
            if weight > 1:
                weight /= 100
            dimensions.append({
                "id": f"d{sheet.cell(row, 1).value}", "name": sheet.cell(row, 2).value,
                "weight": weight, "criterion": sheet.cell(row, 4).value,
                "anchors": [sheet.cell(row, c).value for c in range(5, 10)],
                "source": {"file": book_path.name, "sheet": sheet.title, "range": f"B{row}:I{row}"},
            })
        assert abs(sum(d['weight'] for d in dimensions) - 1) < 1e-9
        if route == 'grammar_choice':
            dimensions[3]['projection'] = 'grammar_distractors'
        if route == 'cloze_whole':
            dimensions[2]['criterion'] = '以五档描述为准；不得使用原D6的跨段百分比区间或其覆盖规则。未覆盖比例不擅自补齐。'
            dimensions[2]['projection'] = 'cloze_span'
        if route == 'seven_whole':
            dimensions[3]['criterion'] = '以五档描述词数为准，300词归5分。'
            dimensions[3]['anchors'][4] = '≥300词，文章与选项均长'
            dimensions[3]['projection'] = 'seven_length'
        rubric = {"version": "2026-10-14.v2" if route == 'grammar_choice' else "2026-10-14.v1", "route": route, "grain": grain,
                  "scope": sheet.cell(2, 1).value, "dimensions": dimensions,
                  "source_rows": [[sheet.cell(r, c).value for c in range(1, 10)] for r in range(1, sheet.max_row + 1)]}
        write(ROOT / 'config' / 'rubrics' / f'{route}.json', rubric)
        routes.append({"id": route, "grain": grain, "rubric": f"rubrics/{route}.json", "scope": rubric['scope']})
    write(ROOT / 'config' / 'routes.json', {"version": "2026-10-14.v1", "routes": routes})
    write(ROOT / 'resources' / 'general_rules.json', {
        "version": "2026-10-14.v1", "source": book_path.name,
        "rows": [[c.value for c in row] for row in book.worksheets[0].iter_rows()],
    })
    rows = []
    pdf_path = args.source_dir / '高考3500(1).pdf'
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            for table_no, table in enumerate(page.extract_tables(), 1):
                for row_no, cells in enumerate(table, 1):
                    if len(cells) >= 4 and cells[-1] and cells[-1].strip().isdigit():
                        rows.append({"lemma": cells[0].strip().lower(), "gloss": cells[1],
                                     "frequency": int(cells[-1]), "page": page_no,
                                     "table": table_no, "row": row_no})
    assert len(rows) == 3500, f'Expected 3500 entries, got {len(rows)}'
    write(ROOT / 'resources' / 'vocabulary.json', {"version": "provided-3500-2026-10-14", "source": pdf_path.name, "entries": rows})
    # Scanned rare-sense examples were visually transcribed and versioned separately.
    # Do not overwrite those curated comparisons when reimporting the XLSX/vocabulary.
    print(json.dumps({"routes": len(routes), "dimensions": sum(len(json.loads((ROOT/'config'/r['rubric']).read_text(encoding='utf-8'))['dimensions']) for r in routes), "vocabulary_rows": len(rows)}, ensure_ascii=False))

if __name__ == '__main__':
    main()
