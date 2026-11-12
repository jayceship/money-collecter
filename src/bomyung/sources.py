"""판매자(재고 보유자) 채널: ① 전국등록공장현황 CSV  ② 온비드 동산 공매 API.

① 공공데이터포털 '한국산업단지공단_전국등록공장현황'(data.go.kr/data/15106170) CSV를 가져와
   전자부품 관련 제조업체를 골라 점수를 매긴다. 연락은 우편 DM/방문용 목록과 안내문으로만 내보낸다
   (이메일·문자·전화 영업은 정보통신망법 제50조 사전 동의 대상).
② 온비드 '공고목록'(OnbidPbancListSrvc2) → '공고상세 물건정보'(OnbidPbancCltrDtlSrvc2)로
   동산 공매 물건 중 전자부품 관련 물건을 찾아 매입처를 붙인다.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx

from . import config, leads

SCHEMA = """
CREATE TABLE IF NOT EXISTS factories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT,
    region TEXT,
    industry_code TEXT,
    industry TEXT,
    products TEXT,
    employees INTEGER,
    score INTEGER DEFAULT 0,
    reason TEXT,
    status TEXT DEFAULT '신규',
    memo TEXT,
    imported_at TEXT,
    UNIQUE(name, address)
);
CREATE TABLE IF NOT EXISTS onbid_items (
    cltr_mng_no TEXT PRIMARY KEY,
    pbanc_mng_no TEXT,
    cltr_no TEXT,
    name TEXT,
    usage TEXT,
    prpt TEXT,
    address TEXT,
    org TEXT,
    notice_name TEXT,
    bid_start TEXT,
    bid_end TEXT,
    bid_status TEXT,
    matched_buyers TEXT,
    status TEXT DEFAULT '신규',
    memo TEXT,
    found_at TEXT
);
CREATE TABLE IF NOT EXISTS onbid_notices (
    pbanc_mng_no TEXT PRIMARY KEY,
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS api_usage (
    api TEXT, day TEXT, calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(api, day)
);
"""

STATUSES = ("신규", "우편발송", "방문", "응답옴", "진행중", "거절", "제외")


def connect() -> sqlite3.Connection:
    con = leads.connect()
    con.executescript(SCHEMA)
    return con


class SourceError(Exception):
    pass


# ====================================================================== ① 공장 목록

# 표준산업분류(KSIC): 261 반도체, 262 전자부품(2622 PCB, 2629 기타), 263~265 컴퓨터·통신·영상음향, 28 전기장비
_CODE_SCORE = (("2612", 45), ("2611", 45), ("2622", 55), ("2629", 55), ("262", 50), ("261", 45),
               ("263", 35), ("264", 35), ("265", 30), ("26", 30), ("285", 20), ("28", 15))
_PRODUCT_HOT = re.compile(r"SMT|PBA|PCBA|EMS|실장|PCB\s*조립|기판\s*조립|모듈|전자부품|반도체|커넥터|MLCC|"
                          r"콘덴서|커패시터|저항기|인덕터|센서|(?<![A-Za-z])IC(?![A-Za-z])|인쇄회로|반도체|LED|파워\s*서플라이|SMPS|전원\s*공급", re.I)
# 2025-12 판 CSV(회사명·단지명·생산품·공장주소, 22만 행) 실측 기준으로 나눔
_PROD_A = re.compile(r"SMT|SMD|(?<![거욕장])실장(?!거울)|PBA|PCBA|EMS|PCB\s*(조립|ASS)|기판\s*조립|인쇄\s*회로|PCB|전자\s*부품|커넥터|"
                     r"MLCC|콘덴서|커패시터|캐패시터|저항기|인덕터|다이오드|트랜지스터|"
                     r"(?<![A-Za-z])IC(?![A-Za-z]|\s*TEST)|반도체(?!\s*(제조\s*)?(장비|장치|설비|용|부품\s*가공|웨이퍼\s*제조))", re.I)
_PROD_B = re.compile(r"전자|모듈|센서|SMPS|전원\s*공급|파워\s*서플라이|컨트롤러|제어기|통신\s*장비|회로|LED|"
                     r"무선|블랙박스|셋톱|충전기", re.I)
_PROD_EQUIP = re.compile(r"장비|장치|설비|기계|조명|가로등|등기구|간판|금속|휠타|필터|진공")
_ELEC_TEXT = re.compile(r"전자|반도체|인쇄회로|PCB|기판|부품|통신|컴퓨터|센서|모듈|커넥터|콘덴서|LED", re.I)


def _find_col(header: list[str], *keys: str) -> int | None:
    for k in keys:
        for i, h in enumerate(header):
            if k in h.replace(" ", ""):
                return i
    return None


def _read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SourceError("CSV 인코딩을 읽지 못했습니다 (UTF-8/CP949만 지원).")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SourceError("빈 파일입니다.")
    return [h.strip() for h in rows[0]], rows[1:]


def _factory_score(code: str, industry: str, products: str, employees: int | None) -> tuple[int, str]:
    s, why = 0, []
    for prefix, pts in _CODE_SCORE:
        if code.startswith(prefix):
            s, why = pts, [f"업종 {code}"]
            break
    if not s and industry and _ELEC_TEXT.search(industry):
        s, why = 25, ["업종명 전자 관련"]
    p = products or ""
    tier_a = sorted({m.group(0).upper().replace(" ", "") for m in _PROD_A.finditer(p)})
    tier_b = sorted({m.group(0).upper().replace(" ", "") for m in _PROD_B.finditer(p)})
    equip = bool(_PROD_EQUIP.search(p))
    if tier_a:
        # 부품을 직접 쓰거나 만드는 곳: 잉여 부품 재고가 생길 가능성이 높음
        add = 60 + 8 * (len(tier_a) - 1) + (5 if tier_b else 0) - (10 if equip else 0)
        s = max(s, min(95, add)) if s else min(95, add)
        why.append("부품 " + ", ".join(tier_a[:4]))
    elif tier_b:
        add = 35 + 5 * (len(tier_b) - 1) - (15 if equip else 0)
        s = max(s, add)
        why.append("전자 " + ", ".join(tier_b[:4]) + (" (장비·조명류 감점)" if equip else ""))
    elif not s and _ELEC_TEXT.search(p):
        s, why = 25, ["생산품 전자 관련(약)"]
    if employees and employees >= 50:
        s += 10
        why.append(f"종업원 {employees}명")
    return min(100, s), " · ".join(why)


def import_factories(path: str | Path, log=print) -> tuple[int, int]:
    """CSV를 읽어 전자 관련 업체만 저장. (검사 행 수, 저장된 신규 수)"""
    header, rows = _read_rows(Path(path))
    c_name = _find_col(header, "회사명", "업체명", "상호")
    c_addr = _find_col(header, "공장대표주소(도로명)", "도로명", "공장주소", "주소")
    c_code = _find_col(header, "대표업종", "업종코드")
    c_ind = _find_col(header, "업종명", "단지명")
    c_prod = _find_col(header, "생산품", "주생산품", "제품")
    c_emp = _find_col(header, "종업원합계", "종업원", "고용")
    if c_name is None or c_addr is None or (c_code is None and c_prod is None):
        raise SourceError("회사명·주소·생산품 열을 찾지 못했습니다. 공공데이터포털 '전국등록공장현황' CSV인지 확인하세요.\n"
                          f"찾은 열: {', '.join(header[:15])}")

    is_ind = c_ind is not None and "업종" in header[c_ind]

    def cell(r, i):
        return r[i].strip() if i is not None and i < len(r) else ""

    con = connect()
    now = datetime.now().isoformat(timespec="seconds")
    new = kept = 0
    try:
        for r in rows:
            code = re.sub(r"\D", "", cell(r, c_code))
            industry, products = cell(r, c_ind), cell(r, c_prod)
            try:
                emp = int(float(cell(r, c_emp).replace(",", ""))) if cell(r, c_emp) else None
            except ValueError:
                emp = None
            score, why = _factory_score(code, industry if is_ind else "", products, emp)
            if score < 25:
                continue
            kept += 1
            addr = cell(r, c_addr)
            cur = con.execute(
                "INSERT INTO factories(name,address,region,industry_code,industry,products,employees,score,reason,imported_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name,address) DO UPDATE SET industry_code=excluded.industry_code,"
                " industry=excluded.industry, products=excluded.products, employees=excluded.employees,"
                " score=excluded.score, reason=excluded.reason",
                (cell(r, c_name), addr, addr.split(" ")[0] if addr else "", code, industry, products, emp, score, why, now))
            new += cur.rowcount
        con.commit()
    finally:
        con.close()
    log(f"{len(rows):,}행 검사 → 전자 관련 {kept:,}곳 저장")
    return len(rows), kept


LETTER = """봉명 재고회수 — 전자부품 잉여재고 매각 안내

{name} 담당자님께

안녕하세요. 전자부품 잉여·장기재고의 매각을 돕는 봉명 재고회수입니다.
생산 종료, 설계 변경, 과발주 등으로 창고에 남은 IC·수동부품·커넥터·모듈 재고가 있으시면
품번과 수량 목록(엑셀 또는 사진)만 보내 주십시오.

· 목록은 무료로 정리해 드리고, 여러 전자부품 매입업체의 견적을 비교해 드립니다.
· 수수료는 매각이 성사된 경우에만 협의합니다. 매입가나 성사를 미리 약속드리지는 않습니다.
· 회사 재고를 처분할 권한이 있는 분과 진행하며, 받은 자료는 견적 비교 외에 쓰지 않습니다.

연락처: {contact}

이 안내가 필요 없으시면 위 연락처로 알려 주십시오. 다시 보내지 않겠습니다.
"""


def export_mailing(ids: list[int], out_dir: str | Path, contact: str) -> Path:
    """우편 DM용: 주소 라벨 CSV + 업체별 안내문 TXT 한 파일."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        q = ",".join("?" * len(ids))
        rows = con.execute(f"SELECT * FROM factories WHERE id IN ({q}) ORDER BY region, name", ids).fetchall()
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        label = out / f"우편주소_{stamp}.csv"
        with label.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["회사명", "주소", "업종", "생산품"])
            for r in rows:
                w.writerow([r["name"], r["address"], r["industry"], r["products"]])
        letters = out / f"안내문_{stamp}.txt"
        letters.write_text("\n\n" + ("\f\n".join(LETTER.format(name=r["name"], contact=contact or "(연락처 입력)")
                                                for r in rows)), encoding="utf-8-sig")
        con.execute(f"UPDATE factories SET status='우편발송' WHERE id IN ({q}) AND status='신규'", ids)
        con.commit()
    finally:
        con.close()
    return label


# ====================================================================== ② 온비드

ONBID_BASE = "https://apis.data.go.kr/B010003"
PRPT_ALL = "0007,0010,0005,0004,0002,0003,0006,0008,0011,0013"
DEFAULT_ONBID_DAILY_LIMIT = 300  # 개발계정 일 1,000건 중 일부만
_ONBID_ELEC = re.compile(r"전자|부품|반도체|(?<![A-Za-z])IC(?![A-Za-z])|PCB|기판|칩|커넥터|콘덴서|커패시터|저항|MLCC|"
                         r"인덕터|다이오드|트랜지스터|모듈|센서|LED|릴|트레이", re.I)


def onbid_daily_limit() -> int:
    try:
        return max(0, int(config.get_setting("onbid_daily_limit", DEFAULT_ONBID_DAILY_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_ONBID_DAILY_LIMIT


def calls_today(con, api: str) -> int:
    row = con.execute("SELECT calls FROM api_usage WHERE api=? AND day=?", (api, date.today().isoformat())).fetchone()
    return row["calls"] if row else 0


def _reserve(con, api: str, limit: int) -> bool:
    d = date.today().isoformat()
    con.execute("INSERT OR IGNORE INTO api_usage(api,day,calls) VALUES (?,?,0)", (api, d))
    ok = con.execute("UPDATE api_usage SET calls=calls+1 WHERE api=? AND day=? AND calls<?", (api, d, limit)).rowcount
    con.commit()
    return bool(ok)


def _items(obj) -> list[dict]:
    """data.go.kr 응답(response.body.items.item 등) 어디에 있든 dict 목록을 꺼낸다."""
    if isinstance(obj, dict):
        for k in ("item", "items"):
            v = obj.get(k)
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v
            if isinstance(v, dict) and ("pbancMngNo" in v or "cltrMngNo" in v):
                return [v]
        for v in obj.values():
            got = _items(v)
            if got:
                return got
    return []


def _check(body: dict) -> None:
    hdr = body.get("response", {}).get("header", body.get("header", {})) if isinstance(body, dict) else {}
    code = str(hdr.get("resultCode", "00"))
    if code not in ("00", "0", "000"):
        raise SourceError(f"온비드 API 오류 {code}: {hdr.get('resultMsg', '')}")


def _get(con, path: str, params: dict, key: str, limit: int) -> dict | None:
    if not _reserve(con, "onbid", limit):
        return None
    r = httpx.get(f"{ONBID_BASE}/{path}", params={"serviceKey": key, "resultType": "json", **params}, timeout=30)
    if r.status_code in (401, 403):
        raise SourceError("온비드 인증키가 거부됐습니다. 활용신청 승인 여부와 키(Decoding 키)를 확인하세요.")
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:
        txt = r.text[:300]
        if "SERVICE_KEY" in txt or "SERVICE KEY" in txt.upper():
            raise SourceError("온비드 인증키가 등록되지 않았거나 아직 활성화되지 않았습니다(승인 후 1~2시간 걸릴 수 있음).")
        raise SourceError(f"온비드 응답을 읽지 못했습니다: {txt}")
    _check(body)
    return body


def run_onbid(days_ahead: int = 30, log=print) -> tuple[int, int]:
    """(확인한 신규 공고 수, 찾은 전자 관련 물건 수)"""
    key = config.get("onbid_service_key")
    if not key:
        raise SourceError("설정 탭에서 공공데이터포털(온비드) 인증키를 입력하세요.")
    key = unquote(key.strip())  # Encoding 키를 넣어도 동작하도록
    limit = onbid_daily_limit()
    con = connect()
    try:
        if calls_today(con, "onbid") >= limit:
            raise SourceError(f"오늘 온비드 호출 한도({limit}회)를 다 썼습니다.")
        today = date.today()
        notices: list[dict] = []
        page = 1
        while page <= 10:
            body = _get(con, "OnbidPbancListSrvc2/getPbancList2", {
                "pageNo": page, "numOfRows": 100, "cltrTypeCd": "0003", "prptDivCd": PRPT_ALL,
                "dspsMthodCd": "0001",
                "opbdDtStart": today.strftime("%Y%m%d"),
                "opbdDtEnd": (today + timedelta(days=days_ahead)).strftime("%Y%m%d")}, key, limit)
            if body is None:
                log("호출 한도 도달 — 중단")
                break
            got = _items(body)
            notices += got
            log(f"동산 공고 {len(notices)}건 수집")
            if len(got) < 100:
                break
            page += 1
        seen = {r[0] for r in con.execute("SELECT pbanc_mng_no FROM onbid_notices")}
        fresh = [n for n in {n.get("pbancMngNo"): n for n in notices if n.get("pbancMngNo")}.values()
                 if n["pbancMngNo"] not in seen]
        now = datetime.now().isoformat(timespec="seconds")
        found = checked = 0
        for n in fresh:
            body = _get(con, "OnbidPbancCltrDtlSrvc2/getPbancCltrInf2",
                        {"pageNo": 1, "numOfRows": 100, "pbancMngNo": n["pbancMngNo"]}, key, limit)
            if body is None:
                log("호출 한도 도달 — 나머지 공고는 다음 실행 때 확인")
                break
            checked += 1
            for it in _items(body):
                text = " ".join(str(it.get(k) or "") for k in (
                    "onbidCltrNm", "cltrUsgLclsCtgrNm", "cltrUsgMclsCtgrNm", "cltrUsgSclsCtgrNm"))
                if not it.get("cltrMngNo") or not _ONBID_ELEC.search(text):
                    continue
                cats = sorted({m.group(0) for m in _ONBID_ELEC.finditer(text)})
                buyers = ", ".join(leads.match_buyers(cats, text))
                cur = con.execute(
                    "INSERT OR IGNORE INTO onbid_items(cltr_mng_no,pbanc_mng_no,cltr_no,name,usage,prpt,address,org,"
                    "notice_name,bid_start,bid_end,bid_status,matched_buyers,found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(it["cltrMngNo"]), n["pbancMngNo"], str(it.get("onbidCltrno") or ""),
                     it.get("onbidCltrNm"), " > ".join(filter(None, (it.get("cltrUsgLclsCtgrNm"),
                     it.get("cltrUsgMclsCtgrNm"), it.get("cltrUsgSclsCtgrNm")))),
                     it.get("prptDivNm") or n.get("prptDivNm"), it.get("cltrAdr"), n.get("orgNm"),
                     n.get("onbidPbancNm"), it.get("cltrBidBgngDt") or n.get("cltrBidBgngDt"),
                     it.get("cltrBidEndDt") or n.get("cltrBidEndDt"), it.get("pbctStatNm"), buyers, now))
                found += cur.rowcount
            con.execute("INSERT OR REPLACE INTO onbid_notices VALUES (?,?)", (n["pbancMngNo"], now))
            con.commit()
            log(f"공고 {checked}/{len(fresh)} 확인 · 전자 관련 {found}건")
        return checked, found
    finally:
        con.close()
