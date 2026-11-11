"""CSV/XLSX 내보내기. 수식 주입을 막기 위해 위험 접두 문자를 이스케이프한다."""

from __future__ import annotations

import csv
import json
from collections import OrderedDict
from pathlib import Path

from .models import FIELD_LABELS_KO, ITEM_FIELDS, ProcessResult

_DANGEROUS_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(v) -> str:
    """스프레드시트가 수식으로 해석할 수 있는 값 앞에 작은따옴표를 붙인다."""
    if v is None:
        return ""
    s = str(v)
    if s.startswith(_DANGEROUS_PREFIX):
        return "'" + s
    return s


def item_rows(result: ProcessResult) -> tuple[list[str], list[list[str]]]:
    header = ["line_no"]
    for f in ITEM_FIELDS:
        header += [f"{f}_raw", f"{f}"]
    header += ["source_sheet", "source_row", "file_sha256"]
    rows = []
    for it in result.items:
        row = [str(it.line_no)]
        for f in ITEM_FIELDS:
            fv = it.fields.get(f)
            row += [fv.raw if fv else None, fv.normalized if fv else None]
        row += [it.row_source.sheet, it.row_source.row, it.row_source.file_sha256]
        rows.append([safe_cell(v) for v in row])
    return header, rows


def issue_rows(result: ProcessResult) -> tuple[list[str], list[list[str]]]:
    header = ["severity", "code", "line_no", "field", "message", "source", "question"]
    rows = [[safe_cell(v) for v in (
        i.severity.value, i.code, i.line_no, FIELD_LABELS_KO.get(i.field or "", i.field),
        i.message, i.source.label() if i.source else "", i.question)]
        for i in result.issues]
    return header, rows


def question_rows(result: ProcessResult) -> tuple[list[str], list[list[str]]]:
    """같은 질문 유형을 묶어 소유자에게 보낼 질문 목록을 만든다 (발송은 하지 않음)."""
    grouped: OrderedDict[tuple, list] = OrderedDict()
    singles: list[str] = []
    for i in result.issues:
        if not i.question:
            continue
        if i.code == "MISSING_FIELD" and i.field:
            grouped.setdefault((i.code, i.field), []).append(i.line_no)
        else:
            singles.append(i.question)
    out = []
    for (code, field), lines in grouped.items():
        label = FIELD_LABELS_KO.get(field, field)
        ln = ", ".join(str(x) for x in lines[:30]) + (" 외" if len(lines) > 30 else "")
        out.append(f"{label} 값이 없습니다 ({len(lines)}개 행: {ln}). 해당 값을 알려주세요.")
    out += list(OrderedDict.fromkeys(singles))
    return ["no", "question"], [[str(n), safe_cell(q)] for n, q in enumerate(out, start=1)]


def summary(result: ProcessResult) -> dict:
    d = result.document
    by_sev: dict[str, int] = {}
    for i in result.issues:
        by_sev[i.severity.value] = by_sev.get(i.severity.value, 0) + 1
    return {
        "file_name": d.file_name,
        "sha256": d.sha256,
        "size_bytes": d.size_bytes,
        "kind": d.kind,
        "received_at": d.received_at.isoformat(),
        "items": len(result.items),
        "issues": by_sev,
        "unmapped_columns": result.unmapped_columns,
        "lot_owner": result.lot.owner,
        "lot_sale_authority_evidence": result.lot.sale_authority_evidence,
        "lot_share_allowed": result.lot.share_allowed,
        "ready_for_quote_request": result.tradeable,
        "note": "P0 결과물. 견적·거래·수수료 상태가 아니며 외부 발송은 하지 않았습니다.",
    }


def write_csv(result: ProcessResult, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(result.document.file_name).stem
    written = []
    for name, (header, rows) in (
        ("items", item_rows(result)),
        ("issues", issue_rows(result)),
        ("questions", question_rows(result)),
    ):
        p = out_dir / f"{stem}.{name}.csv"
        with p.open("w", newline="", encoding="utf-8-sig") as f:  # BOM: 엑셀 한글 호환
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        written.append(p)
    p = out_dir / f"{stem}.summary.json"
    p.write_text(json.dumps(summary(result), ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(p)
    return written


def write_xlsx(result: ProcessResult, out_dir: Path) -> Path:
    from openpyxl import Workbook

    out_dir.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for name, (header, rows) in (
        ("items", item_rows(result)),
        ("issues", issue_rows(result)),
        ("questions", question_rows(result)),
    ):
        ws = wb.create_sheet(name)
        ws.append(header)
        for r in rows:
            ws.append(r)  # safe_cell 처리로 '='로 시작하는 문자열이 없음 → 수식으로 저장되지 않음
    ws = wb.create_sheet("summary")
    for k, v in summary(result).items():
        ws.append([k, safe_cell(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)])
    if result.raw_text_pages:
        ws = wb.create_sheet("pdf_text")
        ws.append(["page", "text"])
        for page, text in result.raw_text_pages:
            ws.append([page, safe_cell(text[:32000])])
    p = out_dir / f"{Path(result.document.file_name).stem}.processed.xlsx"
    wb.save(p)
    return p
