"""API 키는 Windows 자격 증명 관리자(keyring)에 저장한다. 파일·EXE에 넣지 않는다."""

from __future__ import annotations

import os
from pathlib import Path

import keyring

SERVICE = "BomyungRecovery"
KEYS = ("naver_client_id", "naver_client_secret", "anthropic_api_key")

DEFAULT_MODEL = "claude-haiku-5-5"  # 분류용 저가 모델. 필요 시 설정에서 변경
DAILY_AI_CALL_LIMIT = 300            # 일 호출 상한 (예산 보호)


def get(name: str) -> str | None:
    env = os.environ.get(name.upper())
    if env:
        return env
    try:
        return keyring.get_password(SERVICE, name)
    except Exception:  # noqa: BLE001
        return None


def set_(name: str, value: str) -> None:
    if value:
        keyring.set_password(SERVICE, name, value)


DEFAULT_NAVER_DAILY_LIMIT = 200  # 하루 네이버 검색 호출 상한 (실행 1회 ≈ 20회)


def _settings_path() -> Path:
    return data_dir() / "settings.json"


def get_setting(name: str, default=None):
    import json
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8")).get(name, default)
    except (OSError, ValueError):
        return default


def set_setting(name: str, value) -> None:
    import json
    p = _settings_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[name] = value
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def naver_daily_limit() -> int:
    try:
        return max(0, int(get_setting("naver_daily_limit", DEFAULT_NAVER_DAILY_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_NAVER_DAILY_LIMIT


def data_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home())) / "Bomyung"
    base.mkdir(parents=True, exist_ok=True)
    return base
