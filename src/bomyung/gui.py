"""더블클릭 실행용 GUI (tkinter): 재고 정리 / 리드 찾기 / 설정."""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, config, export, leads
from .extract import process
from .intake import IntakeError, accept
from .models import LotMeta, Severity
from .readers import ReadError


def _default_out() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "output"


# ====================================================================== 재고 정리

class InventoryTab(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master, padding=12)
        self.file_var = tk.StringVar()
        self.out_var = tk.StringVar(value=str(_default_out()))
        self.owner_var = tk.StringVar()
        self.auth_var = tk.StringVar()
        self.share_var = tk.StringVar(value="미확인")
        self.last_out: Path | None = None
        self.columnconfigure(1, weight=1)

        def row(r, label, var, btn=None):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(self, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6)
            if btn:
                ttk.Button(self, text="찾기", command=btn).grid(row=r, column=2)

        row(0, "재고 파일 (CSV/XLSX/PDF)", self.file_var, self.pick_file)
        row(1, "결과 폴더", self.out_var, self.pick_out)
        row(2, "소유자", self.owner_var)
        row(3, "매각 권한 근거", self.auth_var)
        ttk.Label(self, text="매입처 공유 허용").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(self, textvariable=self.share_var, state="readonly",
                     values=("미확인", "허용", "불허")).grid(row=4, column=1, sticky="w", padx=6)
        b = ttk.Frame(self)
        b.grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(b, text="처리 실행", command=self.run).pack(side="left")
        self.open_btn = ttk.Button(b, text="결과 폴더 열기", command=self.open_out, state="disabled")
        self.open_btn.pack(side="left", padx=6)
        self.log = tk.Text(self, wrap="word", height=18)
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew")
        self.rowconfigure(6, weight=1)
        self.write("재고 파일을 선택하고 '처리 실행'을 누르세요.\n")

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
            os.startfile(self.last_out)  # noqa: S606

    def run(self) -> None:
        path = self.file_var.get().strip()
        if not path:
            messagebox.showwarning("파일 없음", "재고 파일을 선택하세요.")
            return
        share = {"허용": True, "불허": False}.get(self.share_var.get())
        lot = LotMeta(owner=self.owner_var.get().strip() or None,
                      sale_authority_evidence=self.auth_var.get().strip() or None, share_allowed=share)
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
            self.write(f"\n[오류] {type(e).__name__}: {e}\n")
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
            self.write(f"  · {f'[{i.line_no}행] ' if i.line_no else ''}{i.message}\n")
        self.write(f"결과: {xlsx}\n")


# ====================================================================== 리드 찾기

ROLE_KO = {"seller": "판매자", "buyer": "매입업체", "other": "기타", None: "미판별"}


class LeadsTab(ttk.Frame):
    COLS = ("score", "role", "status", "title", "items", "buyers", "source", "date")
    HEAD = ("점수", "구분", "상태", "제목", "품목", "매칭 매입업체", "출처", "작성일")
    WIDTH = (50, 70, 70, 260, 160, 160, 120, 85)

    def __init__(self, master) -> None:
        super().__init__(master, padding=12)
        self.busy = False
        self.filter_var = tk.StringVar(value="판매자")
        self.status_var = tk.StringVar()

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.run_btn = ttk.Button(top, text="리드 찾기 실행", command=self.start)
        self.run_btn.pack(side="left")
        ttk.Label(top, text="  보기:").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.filter_var, state="readonly", width=10,
                          values=("판매자", "매입업체", "전체", "제외 포함"))
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.reload())
        ttk.Button(top, text="새로고침", command=self.reload).pack(side="left", padx=6)
        self.msg = ttk.Label(top, text="")
        self.msg.pack(side="left", padx=8)

        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, pady=8)
        tf = ttk.Frame(pane)
        self.tree = ttk.Treeview(tf, columns=self.COLS, show="headings", selectmode="browse")
        for c, h, w in zip(self.COLS, self.HEAD, self.WIDTH):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_detail())
        self.tree.bind("<Double-1>", lambda e: self.open_link())
        pane.add(tf, weight=3)

        df = ttk.Frame(pane)
        bar = ttk.Frame(df)
        bar.pack(fill="x")
        ttk.Button(bar, text="글 열기", command=self.open_link).pack(side="left")
        ttk.Button(bar, text="연락 초안 복사", command=self.copy_draft).pack(side="left", padx=6)
        ttk.Label(bar, text="상태:").pack(side="left")
        st = ttk.Combobox(bar, textvariable=self.status_var, state="readonly", width=8, values=leads.STATUSES)
        st.pack(side="left")
        st.bind("<<ComboboxSelected>>", lambda e: self.set_status())
        self.detail = tk.Text(df, wrap="word", height=12)
        self.detail.pack(fill="both", expand=True, pady=(6, 0))
        pane.add(df, weight=2)

        self.rows: dict[str, dict] = {}
        self.reload()

    # ---- 데이터
    def reload(self) -> None:
        con = leads.connect()
        try:
            f = self.filter_var.get()
            where = {"판매자": "role='seller' AND status!='제외'",
                     "매입업체": "role='buyer' AND status!='제외'",
                     "전체": "status!='제외'", "제외 포함": "1=1"}[f]
            rows = con.execute(f"SELECT * FROM leads WHERE {where} ORDER BY score DESC, id DESC LIMIT 1000").fetchall()
        finally:
            con.close()
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for r in rows:
            iid = str(r["id"])
            self.rows[iid] = dict(r)
            self.tree.insert("", "end", iid=iid, values=(
                r["score"], ROLE_KO.get(r["role"], r["role"]), r["status"], r["title"], r["items"] or "",
                r["matched_buyers"] or "", r["source_name"] or r["source"], r["post_date"] or ""))
        self.msg.config(text=f"{len(rows)}건")

    def current(self) -> dict | None:
        sel = self.tree.selection()
        return self.rows.get(sel[0]) if sel else None

    def show_detail(self) -> None:
        r = self.current()
        self.detail.delete("1.0", "end")
        if not r:
            return
        self.status_var.set(r["status"])
        self.detail.insert("end",
            f"제목: {r['title']}\n링크: {r['link']}\n출처: {r['source_name']} ({r['source']}) · 작성일 {r['post_date'] or '-'}\n"
            f"판별: {ROLE_KO.get(r['role'])} (신뢰도 {r['confidence'] or 0:.2f}, {r['classified_by'] or '-'}) — {r['reason'] or ''}\n"
            f"품목: {r['items'] or '-'}\n연락 경로: {r['contact_route'] or '원문 확인 필요'}\n"
            f"매칭 매입업체: {r['matched_buyers'] or '-'}\n\n요약: {r['snippet']}\n\n"
            f"── 연락 초안 ──\n{r['draft'] or '(판매자 리드에만 생성)'}\n")

    def open_link(self) -> None:
        r = self.current()
        if r:
            webbrowser.open(r["link"])

    def copy_draft(self) -> None:
        r = self.current()
        if r and r["draft"]:
            self.clipboard_clear()
            self.clipboard_append(r["draft"])
            self.msg.config(text="초안을 복사했습니다")

    def set_status(self) -> None:
        r = self.current()
        if not r:
            return
        con = leads.connect()
        try:
            con.execute("UPDATE leads SET status=? WHERE id=?", (self.status_var.get(), r["id"]))
            con.commit()
        finally:
            con.close()
        r["status"] = self.status_var.get()
        vals = list(self.tree.item(str(r["id"]), "values"))
        vals[2] = r["status"]
        self.tree.item(str(r["id"]), values=vals)

    # ---- 실행 (백그라운드)
    def log(self, s: str) -> None:
        self.after(0, lambda: self.msg.config(text=s.strip()[:80]))

    def start(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.run_btn.config(state="disabled")

        def work():
            try:
                new, n = leads.run(log=self.log)
                text = f"완료: 신규 {new}건, 판별 {n}건"
            except leads.SearchError as e:
                text = f"[중단] {e}"
            except Exception as e:  # noqa: BLE001
                text = f"[오류] {type(e).__name__}: {e}"
            self.after(0, lambda: self.done(text))

        threading.Thread(target=work, daemon=True).start()

    def done(self, text: str) -> None:
        con = leads.connect()
        try:
            text += f" · 오늘 검색 {leads.naver_calls_today(con)}/{config.naver_daily_limit()}회"
        finally:
            con.close()
        self.busy = False
        self.run_btn.config(state="normal")
        self.reload()
        self.msg.config(text=text)
        if text.startswith("["):
            messagebox.showwarning("리드 찾기", text)


# ====================================================================== 설정

class SettingsTab(ttk.Frame):
    FIELDS = (
        ("naver_client_id", "NAVER API HUB Client ID", False),
        ("naver_client_secret", "NAVER API HUB Client Secret", True),
        ("anthropic_api_key", "Anthropic API Key (현재 리드 판별에는 미사용)", True),
        ("anthropic_model", f"AI 모델 (기본 {config.DEFAULT_MODEL})", False),
    )

    def __init__(self, master) -> None:
        super().__init__(master, padding=12)
        self.vars: dict[str, tk.StringVar] = {}
        self.columnconfigure(1, weight=1)
        for r, (k, label, secret) in enumerate(self.FIELDS):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", pady=4)
            v = tk.StringVar()
            ttk.Entry(self, textvariable=v, show="•" if secret else "").grid(row=r, column=1, sticky="ew", padx=6)
            self.vars[k] = v
            ttk.Label(self, text="저장됨" if config.get(k) else "미설정").grid(row=r, column=2, sticky="w")
        r0 = len(self.FIELDS)
        ttk.Label(self, text="네이버 검색 하루 호출 한도 (회)").grid(row=r0, column=0, sticky="w", pady=4)
        self.limit_var = tk.StringVar(value=str(config.naver_daily_limit()))
        ttk.Spinbox(self, from_=0, to=25000, increment=20, textvariable=self.limit_var, width=10).grid(
            row=r0, column=1, sticky="w", padx=6)
        self.usage_lbl = ttk.Label(self, text="")
        self.usage_lbl.grid(row=r0, column=2, sticky="w")
        self.refresh_usage()
        ttk.Button(self, text="저장", command=self.save).grid(row=r0 + 1, column=1, sticky="w", pady=8)
        help_text = (
            "• 네이버 키: 네이버 클라우드 콘솔(console.ncloud.com) → Menu → All Services → Application Services → "
            "NAVER API HUB → 이용 신청 → Application 등록(검색: 블로그·카페글 선택) → Application 선택 → "
            "API 관리 → [인증 정보]에서 Client ID/Secret 복사. 구 developers.naver.com 키는 쓸 수 없습니다.\n"
            "• 하루 호출 한도: '리드 찾기 실행' 1번에 약 20회를 씁니다. 한도에 닿으면 검색이 자동으로 멈춥니다(0 = 검색 금지). "
            "요금 안전장치로 네이버 클라우드 콘솔에서도 이용 한도·한도 초과 알림을 따로 걸어 두세요.\n"
            "• 네이버 검색 결과는 개정 특약 때문에 외부 AI에 넣지 않고 키워드 규칙으로 판별합니다. "
            "연락하지 않은 리드는 21일 뒤 자동 삭제됩니다.\n"
            "• 키는 Windows 자격 증명 관리자에 저장되며 파일로 남지 않습니다. 빈 칸은 기존 값을 유지합니다.\n"
            f"• 리드 DB 위치: {config.data_dir() / 'leads.db'}"
        )
        ttk.Label(self, text=help_text, justify="left", wraplength=680).grid(
            row=r0 + 2, column=0, columnspan=3, sticky="w", pady=12)

    def refresh_usage(self) -> None:
        con = leads.connect()
        try:
            used = leads.naver_calls_today(con)
        finally:
            con.close()
        self.usage_lbl.config(text=f"오늘 사용 {used} / {config.naver_daily_limit()}회")

    def save(self) -> None:
        n = 0
        try:
            lim = int(self.limit_var.get())
            if not 0 <= lim <= 25000:
                raise ValueError
        except ValueError:
            messagebox.showwarning("설정", "하루 한도는 0~25000 사이 숫자로 입력하세요.")
            return
        if lim != config.naver_daily_limit():
            config.set_setting("naver_daily_limit", lim)
            n += 1
        self.refresh_usage()
        for k, v in self.vars.items():
            val = v.get().strip()
            if val:
                config.set_(k, val)
                v.set("")
                n += 1
        messagebox.showinfo("설정", f"{n}개 항목을 저장했습니다. 다시 열면 상태가 갱신됩니다.")


# ======================================================================

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"봉명 재고회수 v{__version__}")
        self.geometry("1000x680")
        self.minsize(760, 520)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        nb.add(LeadsTab(nb), text="리드 찾기")
        nb.add(InventoryTab(nb), text="재고 정리")
        nb.add(SettingsTab(nb), text="설정")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
