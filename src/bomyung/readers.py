"""원본 파일을 (시트, 행번호, 셀 목록) 단위로 읽는다. 수식은 실행하지 않는다."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

MAX_ROWS = 20_000
MAX_COLS = 200


@dataclass
class RawCell:
    value: str | None
    is_formula: bool = False


@dataclass
class RawRow:
    sheet: str | None
    row: int  # 1-based 원본 행번호
    cells: list[RawCell]


class ReadError(Exception):
    pass


def _cell_str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v)
    return s if s.strip() != "" else None


def read_csv(path: Path) -> list[RawRow]:
    data = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ReadError("CSV 인코딩을 판별할 수 없습니다 (UTF-8, CP949 시도).")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows: list[RawRow] = []
    for i, rec in enumerate(csv.reader(io.StringIO(text), dialect), start=1):
        if i > MAX_ROWS:
            raise ReadError(f"행 수가 한도({MAX_ROWS:,})를 넘습니다.")
        # CSV에는 수식이 없다. '='로 시작하는 값도 문자열로만 다룬다.
        rows.append(RawRow(None, i, [RawCell(_cell_str(c)) for c in rec[:MAX_COLS]]))
    return rows


def read_xlsx(path: Path) -> list[RawRow]:
    from openpyxl import load_workbook

    # data_only=False: 수식 원문을 그대로 읽어 '수식 셀'로 표시한다 (계산·실행하지 않음).
    try:
        wb = load_workbook(path, read_only=True, data_only=False)
    except Exception as e:  # noqa: BLE001 - 손상 파일 등
        raise ReadError(f"XLSX를 열 수 없습니다: {e}") from e
    rows: list[RawRow] = []
    try:
        for ws in wb.worksheets:
            for r_idx, rec in enumerate(ws.iter_rows(values_only=True), start=1):
                if len(rows) >= MAX_ROWS:
                    raise ReadError(f"행 수가 한도({MAX_ROWS:,})를 넘습니다.")
                cells = []
                for v in rec[:MAX_COLS]:
                    is_formula = isinstance(v, str) and v.startswith("=")
                    cells.append(RawCell(_cell_str(v), is_formula))
                rows.append(RawRow(ws.title, r_idx, cells))
    finally:
        wb.close()
    return rows


def read_pdf_text(path: Path) -> list[tuple[int, str]]:
    """PDF 텍스트를 페이지별로 추출. 스캔 PDF는 빈 문자열이 나온다 (OCR 미구현)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        return [(i, page.extract_text() or "") for i, page in enumerate(reader.pages, start=1)]
    except Exception as e:  # noqa: BLE001
        raise ReadError(f"PDF를 읽을 수 없습니다: {e}") from e
