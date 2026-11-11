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


def data_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home())) / "Bomyung"
    base.mkdir(parents=True, exist_ok=True)
    return base
