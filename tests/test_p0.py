"""P0 테스트. 모든 fixture는 tmp_path에 생성되는 합성 데이터이며 운영 데이터가 아니다."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from bomyung import export
from bomyung.cli import main
from bomyung.extract import process
from bomyung.intake import IntakeError, accept
from bomyung.models import LotMeta, Severity

HEADER = ["제조사", "품번", "수량", "단위", "포장", "D/C", "상태"]
FULL_LOT = LotMeta(owner="테스트소유자", sale_authority_evidence="test-mandate", share_allowed=True)


def write_csv(path: Path, rows, encoding="utf-8-sig") -> Path:
    with path.open("w", newline="", encoding=encoding) as f:
        csv.writer(f).writerows(rows)
    return path


def write_xlsx(path: Path, rows) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "재고"
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def run(path: Path, lot: LotMeta = FULL_LOT):
    return process(accept(path), lot)


def codes(result, line_no=None):
    return [i.code for i in result.issues if line_no is None or i.line_no == line_no]


# ---------------------------------------------------------------- 접수

def test_rejects_unsupported_extension(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x")
    with pytest.raises(IntakeError):
        accept(p)


def test_rejects_disguised_xlsx(tmp_path):
    p = tmp_path / "a.xlsx"
    p.write_text("not a zip")
    with pytest.raises(IntakeError, match="형식이 아닙니다"):
        accept(p)


def test_rejects_oversize(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER])
    with pytest.raises(IntakeError, match="너무 큽니다"):
        accept(p, max_bytes=5)


def test_hash_is_stable(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER, ["TI", "LM358DR", "100", "EA", "reel", "2420", "미사용"]])
    assert accept(p).sha256 == accept(p).sha256


# ---------------------------------------------------------------- 추출·근거

def test_clean_file_is_tradeable_with_provenance(tmp_path):
    p = write_xlsx(tmp_path / "s.xlsx", [
        ["재고 목록 2026-11"],          # 제목 행 → 헤더 탐지가 건너뛰어야 함
        HEADER,
        ["TI", "LM358DR", "2,500", "EA", "Tape&Reel", "2420", "미사용"],
    ])
    r = run(p)
    assert r.tradeable, [i.message for i in r.issues]
    it = r.items[0]
    assert it.value("quantity") == "2500"
    assert it.fields["quantity"].raw == "2,500"
    assert it.row_source.row == 3 and it.row_source.sheet == "재고"
    assert it.fields["part_number"].source.column == "품번"


def test_cp949_csv(tmp_path):
    p = write_csv(tmp_path / "k.csv",
                  [HEADER, ["삼성", "CL10A106KP8NNNC", "4000", "개", "릴", "2333", "미개봉"]],
                  encoding="cp949")
    r = run(p)
    assert r.items[0].value("unit") == "EA"
    assert r.items[0].value("manufacturer") == "삼성"


# ---------------------------------------------------------------- 품번

def test_one_char_diff_not_merged(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER,
        ["TI", "LM358DR", "100", "EA", "reel", "2420", "new"],
        ["TI", "LM358D", "100", "EA", "tube", "2420", "new"]])
    r = run(p)
    assert len(r.items) == 2
    assert {it.value("part_number") for it in r.items} == {"LM358DR", "LM358D"}
    assert "PN_SIMILAR" in codes(r)


def test_ambiguous_char_flagged_not_corrected(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER,
        ["ST", "STM32F1O3C8T6", "10", "EA", "tray", "2401", "new"]])
    r = run(p)
    assert r.items[0].value("part_number") == "STM32F1O3C8T6"  # O 그대로
    assert "PN_AMBIGUOUS_CHAR" in codes(r)


def test_scientific_notation_part_is_error(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER, ["X", "1.23E+05", "10", "EA", "bag", "2401", "new"]])
    r = run(p)
    assert r.items[0].value("part_number") is None
    assert "PN_SCIENTIFIC" in codes(r)
    assert not r.tradeable


# ---------------------------------------------------------------- 수량·단위

@pytest.mark.parametrize("raw,code", [
    ("1k", "QTY_UNPARSEABLE"),
    ("12.5", "QTY_UNPARSEABLE"),
    ("약 100", "QTY_UNPARSEABLE"),
    ("1,00", "QTY_BAD_SEPARATOR"),
    ("0", "QTY_NONPOSITIVE"),
])
def test_bad_quantity_not_guessed(tmp_path, raw, code):
    p = write_csv(tmp_path / "a.csv", [HEADER, ["TI", "LM358DR", raw, "EA", "reel", "2420", "new"]])
    r = run(p)
    assert r.items[0].value("quantity") is None
    assert code in codes(r)
    assert not r.tradeable


def test_unit_conflict_between_qty_cell_and_unit_column(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER, ["TI", "LM358DR", "3 reel", "EA", "reel", "2420", "new"]])
    r = run(p)
    assert "UNIT_CONFLICT" in codes(r)
    assert not r.tradeable


def test_unit_split_from_qty_cell_when_unit_column_missing(tmp_path):
    hdr = ["제조사", "품번", "수량", "포장", "D/C", "상태"]
    p = write_csv(tmp_path / "a.csv", [hdr, ["TI", "LM358DR", "500pcs", "reel", "2420", "new"]])
    r = run(p)
    it = r.items[0]
    assert it.value("quantity") == "500" and it.value("unit") == "EA"
    assert "UNIT_FROM_QTY_CELL" in codes(r)


def test_container_unit_warns_and_does_not_convert(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER, ["TI", "LM358DR", "3", "REEL", "reel", "2420", "new"]])
    r = run(p)
    assert r.items[0].value("quantity") == "3"
    assert "UNIT_CONTAINER" in codes(r)


# ---------------------------------------------------------------- 누락·권한

def test_missing_fields_become_questions(tmp_path):
    p = write_csv(tmp_path / "a.csv", [["품번", "수량"], ["LM358DR", "100"], ["NE555P", "50"]])
    r = run(p)
    missing = [i for i in r.issues if i.code == "MISSING_FIELD"]
    assert {i.field for i in missing} == {"manufacturer", "unit", "packaging", "date_code", "condition"}
    _, qrows = export.question_rows(r)
    assert any("제조사" in q[1] and "2개 행" in q[1] for q in qrows)


def test_lot_without_authority_or_share_is_blocked(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER, ["TI", "LM358DR", "100", "EA", "reel", "2420", "new"]])
    r = run(p, LotMeta())
    assert {"LOT_NO_OWNER", "LOT_NO_AUTHORITY", "LOT_SHARE_UNKNOWN"} <= set(codes(r))
    assert not r.tradeable
    r2 = run(p, LotMeta(owner="a", sale_authority_evidence="b", share_allowed=False))
    assert "LOT_SHARE_DENIED" in codes(r2) and not r2.tradeable


def test_duplicate_rows_not_summed(tmp_path):
    row = ["TI", "LM358DR", "100", "EA", "reel", "2420", "new"]
    p = write_csv(tmp_path / "a.csv", [HEADER, row, row])
    r = run(p)
    assert len(r.items) == 2
    assert "DUPLICATE_ROW" in codes(r)


# ---------------------------------------------------------------- 수식

def test_xlsx_formula_cell_not_evaluated(tmp_path):
    p = write_xlsx(tmp_path / "f.xlsx", [HEADER, ["TI", "LM358DR", "=10*10", "EA", "reel", "2420", "new"]])
    r = run(p)
    assert r.items[0].value("quantity") is None
    assert "FORMULA_CELL" in codes(r)


def test_export_escapes_formula_injection(tmp_path):
    p = write_csv(tmp_path / "a.csv", [HEADER,
        ["=HYPERLINK(\"http://x\")", "LM358DR", "100", "EA", "+reel", "2420", "@new"]])
    r = run(p)
    out = tmp_path / "out"
    export.write_csv(r, out)
    xlsx = export.write_xlsx(r, out)
    text = (out / "a.items.csv").read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text and "'+reel" in text and "'@new" in text
    wb = load_workbook(xlsx)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f", f"수식 셀이 저장됨: {ws.title}!{c.coordinate}"


# ---------------------------------------------------------------- PDF·CLI

def test_scanned_like_pdf_reports_no_text(tmp_path):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    p = tmp_path / "s.pdf"
    with p.open("wb") as f:
        w.write(f)
    r = run(p)
    assert "PDF_NO_TEXT" in codes(r)
    assert not r.items and not r.tradeable


def test_cli_exit_codes(tmp_path, capsys):
    p = write_csv(tmp_path / "a.csv", [HEADER, ["TI", "LM358DR", "100", "EA", "reel", "2420", "new"]])
    out = str(tmp_path / "o")
    assert main(["process", str(p), "--out", out]) == 1  # 권한 미입력
    assert main(["process", str(p), "--out", out, "--owner", "a", "--authority", "b",
                 "--share", "yes"]) == 0
    assert main(["process", str(tmp_path / "none.csv")]) == 2
    assert (tmp_path / "o" / "a.processed.xlsx").exists()
