import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from google.oauth2 import service_account


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    spreadsheet_id: str
    credentials_info: Dict[str, Any]


def _load_credentials_info() -> Dict[str, Any]:
    """Load service-account credentials without touching Google Sheet formatting."""
    raw = (
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        or os.environ.get("GOOGLE_CREDENTIALS_JSON")
    )
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON 환경변수가 올바른 JSON이 아닙니다.") from exc

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError as exc:
            raise ConfigError(f"GOOGLE_APPLICATION_CREDENTIALS 파일을 찾을 수 없습니다: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"GOOGLE_APPLICATION_CREDENTIALS 파일이 올바른 JSON이 아닙니다: {path}") from exc

    raise ConfigError(
        "구글 서비스 계정 인증 정보가 없습니다. GOOGLE_SERVICE_ACCOUNT_JSON 또는 GOOGLE_APPLICATION_CREDENTIALS를 설정하세요."
    )


def load_config() -> AppConfig:
    spreadsheet_id = os.environ.get("PURCHASE_SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        raise ConfigError("PURCHASE_SPREADSHEET_ID 환경변수가 없습니다.")
    return AppConfig(spreadsheet_id=spreadsheet_id, credentials_info=_load_credentials_info())


def build_credentials(credentials_info: Dict[str, Any]):
    return service_account.Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
