from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup
from dateutil import parser as date_parser


class StatementParseError(ValueError):
    pass


@dataclass(frozen=True)
class StatementGood:
    name: str
    unit: str
    quantity: Decimal
    sum_amount: Decimal


@dataclass(frozen=True)
class StatementData:
    statement_no: str
    purchase_date: str
    total_amount: Decimal
    goods: List[StatementGood]
    raw: Dict[str, Any]


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", str(key).lower())


def _to_decimal(value: Any, default: Optional[Decimal] = None) -> Decimal:
    if value is None or value == "":
        if default is not None:
            return default
        raise StatementParseError("숫자 값이 비어 있습니다.")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    text = str(value).strip()
    # 1,234원 / 1.000 / 0.5 / -10 같은 형태를 숫자로 읽는다.
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        if default is not None:
            return default
        raise StatementParseError(f"숫자로 읽을 수 없습니다: {value}")
    try:
        return Decimal(match.group(0))
    except InvalidOperation as exc:
        raise StatementParseError(f"숫자로 읽을 수 없습니다: {value}") from exc


def decimal_to_sheet_value(value: Decimal) -> int | float:
    """Google Sheets 값 입력용. 정수는 int, 소수는 float로 넣는다."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def normalize_for_match(value: Any) -> str:
    return normalize_text(value).casefold()


def _is_dash_placeholder(value: Any) -> bool:
    """마켓봄에서 단위/수량이 '-'로 표시되는 안내성 행은 품목으로 처리하지 않는다."""
    text = normalize_text(value)
    if not text:
        return False
    # 일반 하이픈(-), en dash(–), em dash(—), 긴 가로줄(―)만 있는 경우를 제외한다.
    return bool(re.fullmatch(r"[-–—―]+", text))


def _walk(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _find_first_by_keys(obj: Any, candidates: List[str]) -> Optional[Any]:
    """candidates를 우선순위 순서대로 찾는다.

    'id', 'no' 같은 범용 키가 statement_no 같은 정확한 키보다
    먼저 잡히지 않도록, 후보별로 전체 JSON을 순회한다.
    """
    nodes = [node for node in _walk(obj) if isinstance(node, dict)]
    for candidate in candidates:
        target = _normalize_key(candidate)
        for node in nodes:
            for key, value in node.items():
                if _normalize_key(key) == target and value not in (None, ""):
                    return value
    return None


def _find_goods(obj: Any) -> List[Dict[str, Any]]:
    for node in _walk(obj):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if _normalize_key(key) == "goods" and isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _parse_date(value: Any) -> str:
    if value is None or value == "":
        raise StatementParseError("거래명세서 매입일을 찾을 수 없습니다.")
    text = normalize_text(value)
    # 날짜만 먼저 안정적으로 추출한다. 예: 2026. 07. 02, 2026/07/02, 2026-07-02
    match = re.search(r"(20\d{2})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", text)
    if match:
        yyyy, mm, dd = match.groups()
        return f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
    try:
        dt = date_parser.parse(text, fuzzy=True)
        return dt.date().isoformat()
    except Exception as exc:
        raise StatementParseError(f"매입일을 날짜로 읽을 수 없습니다: {text}") from exc


def _extract_statement_value(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    tag = soup.find("input", attrs={"name": "statement"})
    if tag and tag.has_attr("value"):
        return str(tag.get("value", ""))

    # HTML이 깨져 있거나 속성 따옴표가 특이한 경우를 대비한 보조 정규식.
    patterns = [
        r"<input[^>]+name=[\"']statement[\"'][^>]+value=[\"'](?P<value>.*?)[\"'][^>]*>",
        r"<input[^>]+value=[\"'](?P<value>.*?)[\"'][^>]+name=[\"']statement[\"'][^>]*>",
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group("value")

    raise StatementParseError('HTML 안에서 hidden input name="statement" 값을 찾을 수 없습니다.')


def _loads_statement_json(value: str) -> Dict[str, Any]:
    candidates = []
    base = value.strip()
    candidates.append(base)
    candidates.append(html.unescape(base))
    candidates.append(unquote(base))
    candidates.append(html.unescape(unquote(base)))

    last_error: Optional[Exception] = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            # JSON 문자열이 한 번 더 감싸진 경우도 처리한다.
            if isinstance(data, str):
                data = json.loads(data)
            if isinstance(data, dict):
                return data
            raise StatementParseError("statement JSON 최상위 값이 객체가 아닙니다.")
        except Exception as exc:  # noqa: BLE001 - 후보별 파싱을 시도해야 한다.
            last_error = exc
    raise StatementParseError("statement 값을 JSON으로 파싱할 수 없습니다.") from last_error


def _get_field(item: Dict[str, Any], candidates: List[str], required: bool = True) -> Any:
    normalized = {_normalize_key(k) for k in candidates}
    for key, value in item.items():
        if _normalize_key(key) in normalized:
            if value not in (None, ""):
                return value
    if required:
        raise StatementParseError(f"goods 항목에서 필요한 필드를 찾을 수 없습니다: {', '.join(candidates)}")
    return None


def _fallback_statement_no_from_url(source_url: Optional[str]) -> Optional[str]:
    if not source_url:
        return None
    path = urlparse(source_url).path.rstrip("/")
    if not path:
        return None
    tail = path.split("/")[-1].strip()
    return tail or None


def _dict_value(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return None


def _extract_marketbom_slip_no(data: Dict[str, Any]) -> str:
    """마켓봄 실제 전표번호를 우선 추출한다.

    마켓봄 statement JSON에서는 링크 끝값(share_key)이 아니라
    slip.trade_slip_de_no 값(S202607020007 형태)이 사람이 보는 전표번호다.
    trade_slip_de_no가 없을 때만 숫자 trade_slip_no를 사용한다.
    """
    slip = data.get("slip") if isinstance(data.get("slip"), dict) else {}
    direct = (
        _dict_value(slip, "trade_slip_de_no")
        or _dict_value(slip, "trade_slip_no")
        or _dict_value(slip, "slip_no")
        or _dict_value(slip, "slipNo")
    )
    text = normalize_text(direct)
    if text:
        return text

    no_value = _find_first_by_keys(
        data,
        [
            "trade_slip_de_no",
            "tradeSlipDeNo",
            "trade_slip_no",
            "tradeSlipNo",
            "statement_no",
            "statementNo",
            "statement_number",
            "statementNumber",
            "slip_no",
            "slipNo",
            "document_no",
            "documentNo",
            "전표번호",
            "거래명세서번호",
            # 아래 범용 키는 마지막 보조 후보로만 둔다.
            "number",
            "no",
        ],
    )
    return normalize_text(no_value) if no_value not in (None, "") else ""


def _extract_marketbom_total_amount(data: Dict[str, Any], goods: List[StatementGood]) -> Decimal:
    """마켓봄 총금액을 amount.total 우선으로 추출한다.

    goods[].sum_amount는 개별 품목 금액이라 전체 총금액으로 쓰면 안 된다.
    """
    amount = data.get("amount") if isinstance(data.get("amount"), dict) else {}
    direct = (
        _dict_value(amount, "total")
        or _dict_value(amount, "sold")
        or _dict_value(amount, "total_amount")
        or _dict_value(amount, "totalAmount")
    )
    if direct not in (None, ""):
        try:
            return _to_decimal(direct)
        except StatementParseError:
            pass

    total_value = _find_first_by_keys(
        data,
        [
            "statement_total_amount",
            "statementTotalAmount",
            "grand_total",
            "grandTotal",
            "total_amount",
            "totalAmount",
            "total",
            "총금액",
            "합계금액",
        ],
    )
    if total_value not in (None, ""):
        try:
            return _to_decimal(total_value)
        except StatementParseError:
            pass
    return sum((g.sum_amount for g in goods), Decimal(0))


def parse_marketbom_statement(html_text: str, source_url: Optional[str] = None) -> StatementData:
    """Parse MarketBom statement HTML.

    기준: hidden input name="statement" 값의 JSON을 읽고, goods 배열의
    name, unit, quantity, sum_amount를 사용한다.
    """
    value = _extract_statement_value(html_text)
    data = _loads_statement_json(value)

    goods_items = _find_goods(data)
    if not goods_items:
        raise StatementParseError("statement JSON에서 goods 배열을 찾을 수 없습니다.")

    date_value = _find_first_by_keys(
        data,
        [
            "date",
            "statement_date",
            "statementDate",
            "purchase_date",
            "purchaseDate",
            "trade_date",
            "tradeDate",
            "issued_at",
            "issuedAt",
            "order_date",
            "orderDate",
            "created_at",
            "createdAt",
            "매입일",
            "거래일자",
            "작성일자",
        ],
    )
    purchase_date = _parse_date(date_value)

    statement_no = _extract_marketbom_slip_no(data)
    if not statement_no:
        # 실제 전표번호가 JSON에 없을 때만 마지막 보조값으로 링크 끝 share_key를 사용한다.
        statement_no = _fallback_statement_no_from_url(source_url) or ""
    if not statement_no:
        raise StatementParseError("거래명세서 전표번호를 찾을 수 없습니다.")

    goods: List[StatementGood] = []
    for item in goods_items:
        name = normalize_text(_get_field(item, ["name", "goods_name", "goodsName", "product_name", "productName", "품명", "상품명"]))
        unit_raw = _get_field(item, ["unit", "unit_name", "unitName", "단위"], required=False)
        quantity_raw = _get_field(item, ["quantity", "qty", "count", "amount_count", "수량"], required=False)

        # 마켓봄 거래명세서에서 단위 또는 수량이 '-'로 표시되는 행은
        # 실제 매입 품목이 아닌 안내/구분 행으로 보고 매입기록·미등록상품 대상에서 제외한다.
        if _is_dash_placeholder(unit_raw) or _is_dash_placeholder(quantity_raw):
            continue

        if quantity_raw in (None, ""):
            raise StatementParseError(f"goods 항목에서 필요한 필드를 찾을 수 없습니다: quantity, qty, count, amount_count, 수량")

        unit = normalize_text(unit_raw)
        quantity = _to_decimal(quantity_raw)
        sum_amount = _to_decimal(_get_field(item, ["sum_amount", "sumAmount", "total_amount", "totalAmount", "amount", "price", "금액", "합계금액"]))
        goods.append(StatementGood(name=name, unit=unit, quantity=quantity, sum_amount=sum_amount))

    if not goods:
        raise StatementParseError("처리할 수 있는 거래명세서 품목이 없습니다. 단위/수량이 '-'인 행은 제외됩니다.")

    total_amount = _extract_marketbom_total_amount(data, goods)

    return StatementData(
        statement_no=statement_no,
        purchase_date=purchase_date,
        total_amount=total_amount,
        goods=goods,
        raw=data,
    )
