"""더블클릭 실행용 GUI (tkinter). 파일 선택 → 처리 → 결과 폴더 열기."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, export
from .extract import process
from .intake import IntakeError, accept
from .models import LotMeta, Severity
from .readers import ReadError


def _default_out() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "output"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"봉명 재고회수 v{__version__}")
        self.geometry("760x560")
        self.minsize(640, 480)

        self.file_var = tk.StringVar()
        self.out_var = tk.StringVar(value=str(_default_out()))
        self.owner_var = tk.StringVar()
        self.auth_var = tk.StringVar()
        self.share_var = tk.StringVar(value="미확인")
        self.last_out: Path | None = None

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        def row(r, label, var, btn=None):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(f, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6)
            if btn:
                ttk.Button(f, text="찾기", command=btn).grid(row=r, column=2)

        row(0, "재고 파일 (CSV/XLSX/PDF)", self.file_var, self.pick_file)
        row(1, "결과 폴더", self.out_var, self.pick_out)
        row(2, "소유자", self.owner_var)
        row(3, "매각 권한 근거", self.auth_var)
        ttk.Label(f, text="매입처 공유 허용").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.share_var, state="readonly",
                     values=("미확인", "허용", "불허")).grid(row=4, column=1, sticky="w", padx=6)

        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(b, text="처리 실행", command=self.run).pack(side="left")
        self.open_btn = ttk.Button(b, text="결과 폴더 열기", command=self.open_out, state="disabled")
        self.open_btn.pack(side="left", padx=6)

        self.log = tk.Text(f, wrap="word", height=18)
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew")
        f.rowconfigure(6, weight=1)
        self.write("파일을 선택하고 '처리 실행'을 누르세요. 외부로 아무것도 전송하지 않습니다.\n")

    def write(self, s: str) -> None:
        self.log.insert("end", s)
        self.log.see("end")

    def pick_file(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("재고 파일", "*.csv *.xlsx *.pdf"), ("모든 파일", "*.*")])
        if p:
            self.file_var.set(p)

    def pick_out(self) -> None:
        p = filedialog.askdirectory()
        if p:
            self.out_var.set(p)

    def open_out(self) -> None:
        if self.last_out and self.last_out.exists():
            os.startfile(self.last_out)  # noqa: S606 - Windows 탐색기 열기

    def run(self) -> None:
        path = self.file_var.get().strip()
        if not path:
            messagebox.showwarning("파일 없음", "재고 파일을 선택하세요.")
            return
        share = {"허용": True, "불허": False}.get(self.share_var.get())
        lot = LotMeta(owner=self.owner_var.get().strip() or None,
                      sale_authority_evidence=self.auth_var.get().strip() or None,
                      share_allowed=share)
        try:
            doc = accept(path)
            result = process(doc, lot)
            out = Path(self.out_var.get())
            export.write_csv(result, out)
            xlsx = export.write_xlsx(result, out)
        except (IntakeError, ReadError) as e:
            self.write(f"\n[거부] {e}\n")
            return
        except Exception as e:  # noqa: BLE001
            self.write(f"\n[오류] 처리 중 예외: {type(e).__name__}: {e}\n")
            return

        self.last_out = out
        self.open_btn.config(state="normal")
        cnt = {s: sum(1 for i in result.issues if i.severity == s) for s in Severity}
        self.write(f"\n=== {doc.file_name} ===\n품목 {len(result.items)}개 · 오류 {cnt[Severity.ERROR]} · "
                   f"경고 {cnt[Severity.WARNING]} · 정보 {cnt[Severity.INFO]}\n")
        if result.unmapped_columns:
            self.write("매핑 안 된 열: " + ", ".join(result.unmapped_columns) + "\n")
        self.write("견적 요청 후보: " + ("가능\n" if result.tradeable else "불가 — 아래 오류 해소 필요\n"))
        for i in [x for x in result.issues if x.severity == Severity.ERROR][:15]:
            loc = f"[{i.line_no}행] " if i.line_no else ""
            self.write(f"  · {loc}{i.message}\n")
        self.write(f"결과: {xlsx}\n")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
