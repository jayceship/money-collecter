"""규칙 기반 필드 추출·정규화·검증.

원칙 (지시서 PART 8·9):
- 원문값은 보존하고 정규화값은 별도로 둔다.
- 0/O, 1/I, 접미사 차이를 자동 교정하지 않는다. 경고만 낸다.
- 유사 품번을 같은 품목으로 합치지 않는다.
- 빠진 값은 None + 질문으로 남긴다. 추정 수량·단위를 채우지 않는다.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import readers
from .models import (
    FIELD_LABELS_KO,
    ITEM_FIELDS,
    REQUIRED_ITEM_FIELDS,
    FieldValue,
    Issue,
    LotItem,
    LotMeta,
    ProcessResult,
    Severity,
    SourceDocument,
    SourceRef,
)

# ---------------------------------------------------------------- 헤더 매핑

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "manufacturer": ("제조사", "메이커", "제조업체", "브랜드", "maker", "mfr", "mfg",
                     "manufacturer", "brand", "vendor"),
    "part_number": ("품번", "부품번호", "파트넘버", "파트번호", "partno", "partnumber",
                    "pn", "mpn", "mfrpn", "mfrpartno", "part"),
    "quantity": ("수량", "재고수량", "qty", "quantity", "stockqty", "onhand"),
    "unit": ("단위", "unit", "uom"),
    # 'package'/'패키지'는 IC 패키지(SOP-8 등) 의미일 수 있어 포장으로 매핑하지 않는다.
    "packaging": ("포장", "포장형태", "포장단위", "packing", "packaging"),
    "date_code": ("datecode", "dc", "데이트코드", "제조일자", "제조년주", "제조시점",
                  "제조연도", "lotdatecode"),
    "condition": ("상태", "보관상태", "condition", "status", "사용여부"),
    "description": ("품명", "설명", "description", "desc", "규격"),
}


def _norm_header(h: str) -> str:
    return re.sub(r"[\s_\-./#()\[\]]", "", h.strip().lower())


_HEADER_INDEX = {_norm_header(s): field for field, syns in _SYNONYMS.items() for s in syns}


def map_header(cells: list[readers.RawCell]) -> dict[int, str]:
    """열 번호 → 표준 필드. 같은 필드가 두 번 나오면 첫 열만 쓴다."""
    out: dict[int, str] = {}
    used: set[str] = set()
    for i, c in enumerate(cells):
        if c.value is None:
            continue
        f = _HEADER_INDEX.get(_norm_header(c.value))
        if f and f not in used:
            out[i] = f
            used.add(f)
    return out


def find_header(rows: list[readers.RawRow], scan: int = 20) -> tuple[int, dict[int, str]] | None:
    """품번 열을 포함하고 매핑 열이 가장 많은 행을 헤더로 본다."""
    best: tuple[int, dict[int, str]] | None = None
    for idx, r in enumerate(rows[:scan]):
        m = map_header(r.cells)
        if "part_number" in m.values() and len(m) >= 2:
            if best is None or len(m) > len(best[1]):
                best = (idx, m)
    return best


# ---------------------------------------------------------------- 정규화

_UNIT_MAP = {
    "ea": "EA", "pcs": "EA", "pc": "EA", "개": "EA", "piece": "EA", "pieces": "EA",
    "reel": "REEL", "reels": "REEL", "릴": "REEL",
    "tray": "TRAY", "trays": "TRAY", "트레이": "TRAY",
    "tube": "TUBE", "tubes": "TUBE", "튜브": "TUBE",
    "box": "BOX", "boxes": "BOX", "박스": "BOX",
    "bag": "BAG", "봉": "BAG",
}
_CONTAINER_UNITS = {"REEL", "TRAY", "TUBE", "BOX", "BAG"}

_QTY_RE = re.compile(r"^\s*([0-9][0-9,]*)(?:\.0+)?\s*([A-Za-z가-힣]*)\s*$")
_AMBIGUOUS_NEAR_DIGIT = re.compile(r"(?<=\d)[OoIl]|[OoIl](?=\d)")
_SCI_NOTATION = re.compile(r"^\d+(\.\d+)?[eE][+-]?\d+$")


def normalize_unit(raw: str) -> str | None:
    return _UNIT_MAP.get(raw.strip().lower())


def _issue(sev, code, msg, item: LotItem | None = None, field=None, src=None, question=None):
    return Issue(
        severity=sev, code=code, message=msg, field=field,
        line_no=item.line_no if item else None,
        source=src or (item.row_source if item else None),
        question=question,
    )


def _process_part_number(item: LotItem, issues: list[Issue]) -> None:
    fv = item.fields.get("part_number")
    if not fv or fv.raw is None:
        return
    raw = fv.raw
    fv.normalized = raw.strip()  # 앞뒤 공백만 제거. 대소문자·문자 교정 없음.
    pn = fv.normalized
    if _SCI_NOTATION.match(pn):
        fv.normalized = None
        issues.append(_issue(
            Severity.ERROR, "PN_SCIENTIFIC",
            f"품번이 지수 표기({pn})입니다. 엑셀 자동 변환으로 원래 품번이 손상됐을 수 있습니다.",
            item, "part_number", fv.source,
            question=f"{item.line_no}행 품번의 원래 표기를 라벨 사진 기준으로 알려주세요."))
        return
    if re.search(r"\s", pn):
        issues.append(_issue(
            Severity.WARNING, "PN_INNER_SPACE",
            f"품번 '{pn}' 안에 공백이 있습니다. 자동 제거하지 않았습니다.",
            item, "part_number", fv.source))
    if _AMBIGUOUS_NEAR_DIGIT.search(pn):
        issues.append(_issue(
            Severity.WARNING, "PN_AMBIGUOUS_CHAR",
            f"품번 '{pn}'에 숫자와 혼동 가능한 문자(O/o/I/l)가 있습니다. 교정하지 않았으니 라벨과 대조하세요.",
            item, "part_number", fv.source,
            question=f"{item.line_no}행 품번 '{pn}'의 O/0, I/1 표기가 라벨과 같은지 확인해 주세요."))


def _process_quantity(item: LotItem, issues: list[Issue]) -> None:
    fv = item.fields.get("quantity")
    if not fv or fv.raw is None:
        return
    abbreviated = re.fullmatch(r"\s*[\d.,]+\s*[kKmM]\s*", fv.raw) is not None
    m = None if abbreviated else _QTY_RE.match(fv.raw)
    if not m:
        fv.normalized = None
        hint = ""
        if abbreviated:
            hint = " (k/M 축약은 자동 환산하지 않습니다)"
        elif "." in fv.raw:
            hint = " (소수 수량)"
        issues.append(_issue(
            Severity.ERROR, "QTY_UNPARSEABLE",
            f"수량 '{fv.raw}'을 정수로 해석할 수 없습니다{hint}.",
            item, "quantity", fv.source,
            question=f"{item.line_no}행 정확한 수량(개수)과 단위를 알려주세요."))
        return
    digits, unit_suffix = m.group(1), m.group(2)
    if "," in digits and not re.fullmatch(r"\d{1,3}(,\d{3})+", digits):
        fv.normalized = None
        issues.append(_issue(
            Severity.ERROR, "QTY_BAD_SEPARATOR",
            f"수량 '{fv.raw}'의 쉼표 위치가 천 단위 구분과 맞지 않습니다.",
            item, "quantity", fv.source,
            question=f"{item.line_no}행 정확한 수량을 알려주세요."))
        return
    qty = int(digits.replace(",", ""))
    if qty <= 0:
        fv.normalized = None
        issues.append(_issue(Severity.ERROR, "QTY_NONPOSITIVE", f"수량이 0 이하입니다: {fv.raw}",
                             item, "quantity", fv.source))
        return
    fv.normalized = str(qty)

    if unit_suffix:
        suffix_unit = normalize_unit(unit_suffix)
        unit_fv = item.fields.get("unit")
        if suffix_unit is None:
            fv.normalized = None  # 단위 의미를 모르면 수량도 확정하지 않는다
            issues.append(_issue(
                Severity.ERROR, "QTY_UNKNOWN_SUFFIX",
                f"수량 '{fv.raw}'에 알 수 없는 단위 '{unit_suffix}'가 붙어 있습니다.",
                item, "quantity", fv.source,
                question=f"{item.line_no}행 수량 단위 '{unit_suffix}'의 의미를 알려주세요."))
        elif unit_fv is None or unit_fv.raw is None:
            # 원문에 있는 단위를 분리한 것이며 추정이 아니다. 근거는 수량 셀.
            item.fields["unit"] = FieldValue(raw=unit_suffix, normalized=suffix_unit, source=fv.source)
            issues.append(_issue(
                Severity.INFO, "UNIT_FROM_QTY_CELL",
                f"단위 '{suffix_unit}'를 수량 셀 '{fv.raw}'에서 분리했습니다.",
                item, "unit", fv.source))
        else:
            col_unit = normalize_unit(unit_fv.raw)
            if col_unit is not None and col_unit != suffix_unit:
                issues.append(_issue(
                    Severity.ERROR, "UNIT_CONFLICT",
                    f"수량 셀 단위({suffix_unit})와 단위 열({col_unit})이 다릅니다.",
                    item, "unit", unit_fv.source,
                    question=f"{item.line_no}행 수량 {qty}의 실제 단위가 무엇인지 알려주세요."))


def _process_unit(item: LotItem, issues: list[Issue]) -> None:
    fv = item.fields.get("unit")
    if not fv or fv.raw is None or fv.normalized is not None:
        return
    u = normalize_unit(fv.raw)
    fv.normalized = u
    if u is None:
        issues.append(_issue(
            Severity.ERROR, "UNIT_UNKNOWN", f"알 수 없는 단위: '{fv.raw}'",
            item, "unit", fv.source,
            question=f"{item.line_no}행 단위 '{fv.raw}'가 개수(EA)인지 포장 단위인지 알려주세요."))


def _check_container_unit(item: LotItem, issues: list[Issue]) -> None:
    unit = item.value("unit")
    if unit in _CONTAINER_UNITS:
        issues.append(_issue(
            Severity.WARNING, "UNIT_CONTAINER",
            f"수량 단위가 {unit}입니다. 개수(EA) 환산은 자동으로 하지 않습니다.",
            item, "unit", item.fields["unit"].source,
            question=f"{item.line_no}행 {unit}당 부품 개수를 알려주세요."))


def _simple_fields(item: LotItem) -> None:
    for f in ("manufacturer", "packaging", "date_code", "condition", "description"):
        fv = item.fields.get(f)
        if fv and fv.raw is not None and fv.normalized is None:
            fv.normalized = fv.raw.strip()


def _edit_distance_le1(a: str, b: str) -> bool:
    if a == b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            j += 1
        else:
            i += 1
            j += 1
    return True


def _cross_item_checks(items: list[LotItem], issues: list[Issue]) -> None:
    seen: dict[tuple, LotItem] = {}
    pns = [(it, it.value("part_number")) for it in items if it.value("part_number")]
    for it in items:
        key = (it.value("manufacturer"), it.value("part_number"), it.value("date_code"),
               it.value("unit"))
        if key[1] is None:
            continue
        if key in seen:
            issues.append(_issue(
                Severity.WARNING, "DUPLICATE_ROW",
                f"{seen[key].line_no}행과 제조사·품번·Date Code·단위가 같습니다. 수량을 합산하지 않았습니다.",
                it, "part_number",
                question=f"{seen[key].line_no}행과 {it.line_no}행이 별도 재고인지 중복 기재인지 알려주세요."))
        else:
            seen[key] = it
    for i, (a, pa) in enumerate(pns):
        for b, pb in pns[i + 1:]:
            if pa == pb:
                continue
            ua, ub = pa.upper(), pb.upper()
            similar = _edit_distance_le1(ua, ub) or (
                (ua.startswith(ub) or ub.startswith(ua)) and min(len(ua), len(ub)) >= 4
            )
            if similar:
                issues.append(_issue(
                    Severity.WARNING, "PN_SIMILAR",
                    f"품번 '{pa}'({a.line_no}행)와 '{pb}'({b.line_no}행)가 유사합니다. "
                    "접미사·1글자 차이는 다른 제품일 수 있어 별도 품목으로 유지했습니다.",
                    b, "part_number"))


# ---------------------------------------------------------------- 메인

def _lot_issues(lot: LotMeta, issues: list[Issue]) -> None:
    if not lot.owner:
        issues.append(Issue(severity=Severity.ERROR, code="LOT_NO_OWNER", field="owner",
                            message="재고 소유자가 입력되지 않았습니다.",
                            question="이 재고의 소유자(법인명 또는 성명)는 누구입니까?"))
    if not lot.sale_authority_evidence:
        issues.append(Issue(severity=Severity.ERROR, code="LOT_NO_AUTHORITY", field="sale_authority",
                            message="매각 권한 근거가 없습니다. 견적 요청 대상이 아닙니다.",
                            question="매각 권한을 확인할 수 있는 문서(위임장·사내 승인 등)를 제공해 주세요."))
    if lot.share_allowed is None:
        issues.append(Issue(severity=Severity.ERROR, code="LOT_SHARE_UNKNOWN", field="share_permission",
                            message="매입처에 자료를 공유해도 되는지 확인되지 않았습니다.",
                            question="이 목록을 매입업체에 견적용으로 공유해도 됩니까? 공유 범위를 알려주세요."))
    elif lot.share_allowed is False:
        issues.append(Issue(severity=Severity.ERROR, code="LOT_SHARE_DENIED", field="share_permission",
                            message="소유자가 자료 공유를 허용하지 않았습니다. 견적 요청을 할 수 없습니다."))


def extract_rows(doc: SourceDocument, rows: list[readers.RawRow],
                 issues: list[Issue]) -> tuple[list[LotItem], list[str]]:
    items: list[LotItem] = []
    unmapped: list[str] = []
    sheets: dict[str | None, list[readers.RawRow]] = {}
    for r in rows:
        sheets.setdefault(r.sheet, []).append(r)

    line_no = 0
    for sheet, srows in sheets.items():
        hdr = find_header(srows)
        if hdr is None:
            if any(any(c.value for c in r.cells) for r in srows):
                issues.append(Issue(
                    severity=Severity.WARNING, code="NO_HEADER",
                    message=f"{'시트 ' + sheet if sheet else '파일'}에서 품번 헤더를 찾지 못해 건너뛰었습니다.",
                    source=SourceRef(file_sha256=doc.sha256, sheet=sheet)))
            continue
        h_idx, colmap = hdr
        header_row = srows[h_idx]
        for i, c in enumerate(header_row.cells):
            if c.value and i not in colmap:
                unmapped.append(f"{sheet + ':' if sheet else ''}{c.value}")
        missing_cols = [f for f in REQUIRED_ITEM_FIELDS if f not in colmap.values()]
        if missing_cols:
            issues.append(Issue(
                severity=Severity.INFO, code="MISSING_COLUMNS",
                message="파일에 없는 열: " + ", ".join(FIELD_LABELS_KO[f] for f in missing_cols),
                source=SourceRef(file_sha256=doc.sha256, sheet=sheet, row=header_row.row)))

        for r in srows[h_idx + 1:]:
            vals = {f: (r.cells[i] if i < len(r.cells) else readers.RawCell(None))
                    for i, f in colmap.items()}
            if all(c.value is None for c in vals.values()):
                continue
            line_no += 1
            item = LotItem(line_no=line_no,
                           row_source=SourceRef(file_sha256=doc.sha256, sheet=sheet, row=r.row))
            for f, cell in vals.items():
                header_name = header_row.cells[[k for k, v in colmap.items() if v == f][0]].value
                src = SourceRef(file_sha256=doc.sha256, sheet=sheet, row=r.row, column=header_name)
                if cell.is_formula:
                    item.fields[f] = FieldValue(raw=cell.value, normalized=None, source=src)
                    issues.append(_issue(
                        Severity.ERROR, "FORMULA_CELL",
                        f"{FIELD_LABELS_KO[f]} 값이 수식({cell.value})입니다. 계산하지 않았습니다.",
                        item, f, src,
                        question=f"{line_no}행 {FIELD_LABELS_KO[f]}의 실제 값을 알려주세요."))
                    continue
                item.fields[f] = FieldValue(raw=cell.value, source=src)
            items.append(item)
    return items, unmapped


def validate_items(items: list[LotItem], issues: list[Issue]) -> None:
    for item in items:
        _process_part_number(item, issues)
        _process_quantity(item, issues)
        _process_unit(item, issues)
        _simple_fields(item)
        _check_container_unit(item, issues)
        for f in REQUIRED_ITEM_FIELDS:
            fv = item.fields.get(f)
            already = any(i.line_no == item.line_no and i.field == f and i.severity == Severity.ERROR
                          for i in issues)
            if (fv is None or fv.raw is None) and not already:
                issues.append(_issue(
                    Severity.ERROR, "MISSING_FIELD",
                    f"필수값 누락: {FIELD_LABELS_KO[f]}",
                    item, f,
                    question=f"{item.line_no}행의 {FIELD_LABELS_KO[f]}을(를) 알려주세요."))
    _cross_item_checks(items, issues)


def process(doc: SourceDocument, lot: LotMeta | None = None) -> ProcessResult:
    lot = lot or LotMeta()
    issues: list[Issue] = []
    path = Path(doc.path)
    result = ProcessResult(document=doc, lot=lot)

    if doc.kind == "pdf":
        pages = readers.read_pdf_text(path)
        result.raw_text_pages = pages
        if not any(t.strip() for _, t in pages):
            issues.append(Issue(severity=Severity.ERROR, code="PDF_NO_TEXT",
                                message="PDF에서 텍스트가 나오지 않습니다. 스캔 이미지일 수 있으며 OCR은 P0에 없습니다.",
                                question="원본 엑셀 또는 텍스트 PDF를 제공할 수 있습니까?"))
        else:
            issues.append(Issue(severity=Severity.ERROR, code="PDF_UNSTRUCTURED",
                                message="PDF 표 구조 추출은 P0 미구현입니다. 원문 텍스트만 보존했습니다. "
                                        "품목 필드는 수동 정리 또는 AI 추출(P1, 미연결)이 필요합니다."))
    else:
        rows = readers.read_csv(path) if doc.kind == "csv" else readers.read_xlsx(path)
        items, unmapped = extract_rows(doc, rows, issues)
        validate_items(items, issues)
        result.items = items
        result.unmapped_columns = unmapped
        if not items:
            issues.append(Issue(severity=Severity.ERROR, code="NO_ITEMS",
                                message="추출된 품목이 없습니다."))

    _lot_issues(lot, issues)
    result.issues = issues
    return result
