"""명령줄 진입점.

예:
  bomyung process 재고목록.xlsx --owner "OO전자" --authority "위임장_2026-11-12.pdf" --share yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import export
from .extract import process
from .intake import IntakeError, accept
from .models import LotMeta, Severity
from .readers import ReadError


def _share(v: str | None) -> bool | None:
    if v is None:
        return None
    v = v.strip().lower()
    if v in ("yes", "y", "true", "1", "허용"):
        return True
    if v in ("no", "n", "false", "0", "불허"):
        return False
    raise argparse.ArgumentTypeError("--share 는 yes 또는 no")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="bomyung", description="봉명 재고회수 P0 파일 처리")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process", help="재고 파일을 정규화하고 오류·질문 목록을 만든다")
    p.add_argument("file")
    p.add_argument("--out", default="output", help="결과 폴더 (기본: output)")
    p.add_argument("--owner", help="재고 소유자")
    p.add_argument("--authority", help="매각 권한 근거 (문서명 등)")
    p.add_argument("--share", type=_share, help="매입처 공유 허용 yes/no (미입력=미확인)")
    p.add_argument("--format", choices=("both", "csv", "xlsx"), default="both")
    args = ap.parse_args(argv)

    try:
        doc = accept(args.file)
        result = process(doc, LotMeta(owner=args.owner, sale_authority_evidence=args.authority,
                                      share_allowed=args.share))
    except (IntakeError, ReadError) as e:
        print(f"[거부] {e}", file=sys.stderr)
        return 2

    out = Path(args.out)
    written: list[Path] = []
    if args.format in ("both", "csv"):
        written += export.write_csv(result, out)
    if args.format in ("both", "xlsx"):
        written.append(export.write_xlsx(result, out))

    counts = {s: sum(1 for i in result.issues if i.severity == s) for s in Severity}
    print(f"파일: {doc.file_name}  sha256={doc.sha256[:16]}…")
    print(f"품목: {len(result.items)}개  오류 {counts[Severity.ERROR]} / 경고 {counts[Severity.WARNING]} "
          f"/ 정보 {counts[Severity.INFO]}")
    if result.unmapped_columns:
        print("매핑 안 된 열: " + ", ".join(result.unmapped_columns))
    print("견적 요청 후보 여부: " + ("가능 (P0 기준)" if result.tradeable else "불가 — 오류 해소 필요"))
    for path in written:
        print(f"  → {path}")
    return 0 if result.tradeable else 1


if __name__ == "__main__":
    raise SystemExit(main())
