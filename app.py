from __future__ import annotations

import logging
import os
from typing import Optional

import requests
from flask import Flask, flash, redirect, render_template, request, url_for

from services.config import ConfigError, load_config
from services.marketbom_parser import StatementParseError, parse_marketbom_statement
from services.processor import ProcessResult, PurchaseProcessor
from services.sheets import PurchaseSheetClient, SheetOperationError, TemplateValidationError


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "purchase-price-local-secret")
logging.basicConfig(level=logging.INFO)


class UserVisibleError(RuntimeError):
    pass


def build_processor() -> PurchaseProcessor:
    config = load_config()
    sheet = PurchaseSheetClient(config.spreadsheet_id, config.credentials_info)
    return PurchaseProcessor(sheet)


def fetch_html_from_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise UserVisibleError("거래명세서 링크를 입력해주세요.")
    if not (url.startswith("https://") or url.startswith("http://")):
        raise UserVisibleError("거래명세서 링크는 http:// 또는 https://로 시작해야 합니다.")
    response = requests.get(
        url,
        timeout=20,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ChaesoFarmPurchasePrice/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    response.raise_for_status()
    # requests는 charset 미지정 시 encoding을 ISO-8859-1로 잡아 한글이 깨진다.
    # 그 경우 실제 내용 기반 추정(apparent_encoding)이나 utf-8로 보정한다.
    if not response.encoding or response.encoding.lower() in ("iso-8859-1", "latin-1"):
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _get_statement_url_from_request() -> str:
    """화면 input name이 바뀌어도 링크를 안정적으로 받는다."""
    form_keys = ("source_url", "statement_url", "url", "link")
    for key in form_keys:
        value = request.form.get(key, "")
        if value and value.strip():
            return value.strip()

    payload = request.get_json(silent=True) or {}
    if isinstance(payload, dict):
        for key in form_keys:
            value = str(payload.get(key, "") or "").strip()
            if value:
                return value
    return ""


def result_from_error(error: Exception) -> ProcessResult:
    return ProcessResult(status="실패", errors=[str(error)])


def handle_exception(exc: Exception) -> ProcessResult:
    if isinstance(exc, (ConfigError, UserVisibleError, StatementParseError, TemplateValidationError, SheetOperationError)):
        app.logger.warning("purchase-price-app user error: %s", exc)
        return result_from_error(exc)
    app.logger.exception("purchase-price-app unexpected error", exc_info=exc)
    return result_from_error(RuntimeError(f"예상하지 못한 오류가 발생했습니다: {exc}"))


@app.get("/")
def index():
    return render_template("index.html", result=None, source_url="")


@app.post("/process-url")
def process_url():
    source_url = _get_statement_url_from_request()
    try:
        html = fetch_html_from_url(source_url)
        statement = parse_marketbom_statement(html, source_url=source_url)
        result = build_processor().process_statement(statement)
    except Exception as exc:  # noqa: BLE001 - 화면에 한글 요약으로 보여준다.
        result = handle_exception(exc)
    return render_template(
        "index.html",
        result=result,
        title="거래명세서 처리 결과",
        # 처리 후에는 같은 링크가 입력칸에 남지 않도록 비운다.
        source_url="",
    )


@app.post("/reprocess-missing")
def reprocess_missing():
    try:
        result = build_processor().reprocess_missing()
    except Exception as exc:  # noqa: BLE001
        result = handle_exception(exc)
    return render_template("index.html", result=result, title="미등록상품 재처리 결과")


@app.post("/bulk-rename")
def bulk_rename():
    try:
        result = build_processor().bulk_rename_products()
    except Exception as exc:  # noqa: BLE001
        result = handle_exception(exc)
    return render_template("index.html", result=result, title="상품명 일괄변경 결과")


@app.post("/delete-records")
def delete_records():
    try:
        date_text = request.form.get("delete_date", "")
        product_name = request.form.get("delete_product", "")
        result = build_processor().delete_purchase_records(date_text, product_name)
    except Exception as exc:  # noqa: BLE001
        result = handle_exception(exc)
    return render_template("index.html", result=result, title="매입기록 삭제 결과")


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
