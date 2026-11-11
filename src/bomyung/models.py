"""P0 데이터 모델. 원문값과 정규화값을 분리하고 모든 값에 근거 위치를 붙인다."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    ERROR = "error"      # 거래 자료로 사용할 수 없음
    WARNING = "warning"  # 사람이 확인해야 함
    INFO = "info"


class SourceRef(BaseModel):
    """값이 나온 원문 위치. 파일 해시로 원본과 연결된다."""

    file_sha256: str
    sheet: str | None = None
    row: int | None = None      # 1-based, 원본 파일 기준
    column: str | None = None   # 원본 헤더명 또는 열 문자
    page: int | None = None     # PDF 1-based

    def label(self) -> str:
        parts = []
        if self.sheet:
            parts.append(f"sheet={self.sheet}")
        if self.page is not None:
            parts.append(f"page={self.page}")
        if self.row is not None:
            parts.append(f"row={self.row}")
        if self.column:
            parts.append(f"col={self.column}")
        return ", ".join(parts)


class FieldValue(BaseModel):
    raw: str | None = None          # 원문 그대로 (수정 금지)
    normalized: str | None = None   # 규칙 정규화 결과. 추정값 금지
    source: SourceRef | None = None


class SourceDocument(BaseModel):
    path: str
    file_name: str
    sha256: str
    size_bytes: int
    kind: str  # csv | xlsx | pdf
    received_at: datetime


class LotMeta(BaseModel):
    """재고 묶음 단위 정보. 파일 안에 없으면 운영자가 근거와 함께 입력한다."""

    owner: str | None = None
    sale_authority_evidence: str | None = None  # 매각 권한 근거 (문서명·위치)
    share_allowed: bool | None = None           # 매입처 공유 허용 여부. None = 미확인


ITEM_FIELDS = (
    "manufacturer",
    "part_number",
    "quantity",
    "unit",
    "packaging",
    "date_code",
    "condition",
    "description",
)

REQUIRED_ITEM_FIELDS = (
    "manufacturer",
    "part_number",
    "quantity",
    "unit",
    "packaging",
    "date_code",
    "condition",
)

FIELD_LABELS_KO = {
    "manufacturer": "제조사",
    "part_number": "품번",
    "quantity": "수량",
    "unit": "단위",
    "packaging": "포장",
    "date_code": "Date Code",
    "condition": "상태",
    "description": "설명",
    "owner": "소유자",
    "sale_authority": "매각 권한",
    "share_permission": "공유 허용",
}


class LotItem(BaseModel):
    line_no: int  # 추출 순번
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    row_source: SourceRef

    def value(self, name: str) -> str | None:
        fv = self.fields.get(name)
        return fv.normalized if fv else None


class Issue(BaseModel):
    severity: Severity
    code: str
    message: str
    field: str | None = None
    line_no: int | None = None
    source: SourceRef | None = None
    question: str | None = None  # 소유자에게 물을 질문


class ProcessResult(BaseModel):
    document: SourceDocument
    lot: LotMeta
    items: list[LotItem] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    unmapped_columns: list[str] = Field(default_factory=list)
    raw_text_pages: list[tuple[int, str]] = Field(default_factory=list)  # PDF 원문

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.ERROR)

    @property
    def tradeable(self) -> bool:
        """P0 기준: 오류가 없고 권한·공유가 확인돼야 견적 요청 후보가 된다."""
        return bool(self.items) and self.error_count == 0
