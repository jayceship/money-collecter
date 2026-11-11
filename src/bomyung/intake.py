"""파일 접수: 형식·크기 제한, 해시 계산. 원본 파일은 수정하지 않는다."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .models import SourceDocument

MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB
ALLOWED = {".csv": "csv", ".xlsx": "xlsx", ".pdf": "pdf"}

# 매직 바이트로 확장자 위장 차단
_MAGIC = {
    "xlsx": b"PK\x03\x04",
    "pdf": b"%PDF",
}


class IntakeError(Exception):
    """접수 거부. 메시지는 사용자에게 그대로 보여줄 수 있다."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def accept(path: str | Path, max_bytes: int = MAX_FILE_BYTES) -> SourceDocument:
    p = Path(path)
    if not p.is_file():
        raise IntakeError(f"파일이 없습니다: {p}")
    kind = ALLOWED.get(p.suffix.lower())
    if kind is None:
        raise IntakeError(
            f"지원하지 않는 형식입니다: {p.suffix or '(확장자 없음)'} — 허용: CSV, XLSX, PDF"
        )
    size = p.stat().st_size
    if size == 0:
        raise IntakeError("빈 파일입니다.")
    if size > max_bytes:
        raise IntakeError(f"파일이 너무 큽니다: {size:,} bytes (한도 {max_bytes:,})")
    magic = _MAGIC.get(kind)
    if magic:
        with p.open("rb") as f:
            if f.read(len(magic)) != magic:
                raise IntakeError(f"확장자는 {kind}이지만 파일 내용이 {kind} 형식이 아닙니다.")
    return SourceDocument(
        path=str(p.resolve()),
        file_name=p.name,
        sha256=sha256_file(p),
        size_bytes=size,
        kind=kind,
        received_at=datetime.now(timezone.utc),
    )
