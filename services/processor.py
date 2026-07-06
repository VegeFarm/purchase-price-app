from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .marketbom_parser import StatementData, StatementGood, decimal_to_sheet_value, normalize_for_match, normalize_text
from .sheets import ConversionRow, PurchaseSheetClient, normalize_cell


KST = ZoneInfo("Asia/Seoul")


@dataclass
class ProcessResult:
    status: str
    statement_no: str = ""
    purchase_date: str = ""
    inserted_count: int = 0
    missing_count: int = 0
    duplicate: str = "신규 입력"
    total_goods: int = 0
    message: str = ""
    errors: List[str] = field(default_factory=list)
    details: List[str] = field(default_factory=list)


@dataclass
class ConversionMatch:
    ok: bool
    converted_name: str = ""
    factor: Decimal = Decimal(0)
    reason: str = ""
    inactive: bool = False


def _today_kst() -> str:
    return datetime.now(KST).date().isoformat()


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            return Decimal(str(value))
        text = str(value).strip().replace(",", "")
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        return Decimal(m.group(0)) if m else None
    except (InvalidOperation, ValueError):
        return None


def _sheet_num(value: Decimal) -> int | float:
    return decimal_to_sheet_value(value)


def _pending_status(reason: str, quantity: Decimal, amount: Decimal) -> str:
    # 별도 수량/금액 컬럼이 없으므로 재처리 가능하도록 처리상태에 최소 메타데이터를 함께 둔다.
    return f"처리대기 | {reason} | 수량={quantity} | 금액={amount}"


def _extract_reason(status: str) -> str:
    parts = [p.strip() for p in str(status).split("|")]
    if len(parts) >= 2 and parts[0] == "처리대기":
        return parts[1]
    return str(status).strip()


def _parse_pending_status(status: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    qty = None
    amount = None
    m_qty = re.search(r"수량\s*=\s*(-?\d+(?:\.\d+)?)", status)
    m_amt = re.search(r"금액\s*=\s*(-?\d+(?:\.\d+)?)", status)
    if m_qty:
        qty = _to_decimal(m_qty.group(1))
    if m_amt:
        amount = _to_decimal(m_amt.group(1))
    return qty, amount


def _normalize_date(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    # Google Sheets의 formatted date가 2026. 7. 2 또는 2026-07-02 등으로 들어와도 맞춘다.
    match = re.search(r"(20\d{2})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", text)
    if match:
        y, m, d = match.groups()
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return text


class PurchaseProcessor:
    def __init__(self, sheet: PurchaseSheetClient):
        self.sheet = sheet

    def _find_conversion(self, good: StatementGood, conversion_rows: List[ConversionRow]) -> ConversionMatch:
        name_key = normalize_for_match(good.name)
        unit_key = normalize_for_match(good.unit)

        same_name = [r for r in conversion_rows if normalize_for_match(r.original_name) == name_key]
        if not same_name:
            return ConversionMatch(ok=False, reason="품목환산표 미등록")

        active_rows = [r for r in same_name if r.active]
        inactive_rows = [r for r in same_name if not r.active]
        if not active_rows and inactive_rows:
            return ConversionMatch(ok=False, reason="미사용", inactive=True)

        unit_matched = []
        for row in active_rows:
            # 품목환산표 단위가 비어 있으면 단위 상관없이 매칭한다.
            if not row.unit or normalize_for_match(row.unit) == unit_key:
                unit_matched.append(row)
        if not unit_matched:
            return ConversionMatch(ok=False, reason="단위 불일치")

        row = unit_matched[0]
        if not row.converted_name:
            return ConversionMatch(ok=False, reason="변환품명 없음")

        factor = _to_decimal(row.factor)
        if factor is None:
            return ConversionMatch(ok=False, reason="환산수량 없음")

        return ConversionMatch(ok=True, converted_name=row.converted_name, factor=factor)

    def _statement_exists(self, statement_no: str) -> Tuple[bool, str]:
        # 숫자형 전표번호가 시트 서식 때문에 "1,234"처럼 읽히는 경우를 대비해
        # 콤마를 제거하고 비교한다.
        target = normalize_cell(statement_no).replace(",", "")
        rows = self.sheet.get_values("'전표관리'!A:C", formatted=True)
        for row in rows[1:]:
            no = normalize_cell(row[0] if len(row) > 0 else "").replace(",", "")
            status = normalize_cell(row[2] if len(row) > 2 else "")
            if no and no == target:
                return True, status or "처리상태 없음"
        return False, ""

    def process_statement(self, statement: StatementData) -> ProcessResult:
        self.sheet.validate_template()
        result = ProcessResult(
            status="성공",
            statement_no=statement.statement_no,
            purchase_date=statement.purchase_date,
            total_goods=len(statement.goods),
        )

        exists, existing_status = self._statement_exists(statement.statement_no)
        if exists:
            result.status = "실패"
            result.duplicate = f"중복 전표({existing_status})"
            result.message = "전표관리에 이미 있는 전표라 매입기록에 다시 입력하지 않았습니다. 재업로드하려면 전표관리에서 해당 행을 삭제하세요."
            return result

        conversion_rows = self.sheet.read_conversion_rows()
        aggregated: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: {"qty": Decimal(0), "amount": Decimal(0)})
        missing_rows: List[List[Any]] = []
        upload_date = _today_kst()

        for good in statement.goods:
            match = self._find_conversion(good, conversion_rows)
            if match.ok:
                converted_qty = good.quantity * match.factor
                aggregated[match.converted_name]["qty"] += converted_qty
                aggregated[match.converted_name]["amount"] += good.sum_amount
            elif match.inactive:
                # 사용여부가 미사용이면 매입기록과 미등록상품에 넣지 않고 건너뜁니다.
                continue
            else:
                status = _pending_status(match.reason, good.quantity, good.sum_amount)
                missing_rows.append([
                    upload_date,
                    statement.statement_no,
                    statement.purchase_date,
                    good.name,
                    good.unit,
                    status,
                ])

        purchase_rows = [
            [statement.purchase_date, product, _sheet_num(data["qty"]), _sheet_num(data["amount"])]
            for product, data in aggregated.items()
        ]
        management_status = "처리완료" if not missing_rows else "처리대기"
        management_rows = [[statement.statement_no, statement.purchase_date, management_status]]

        updates = []
        for prepared in [
            self.sheet.prepare_append_range("매입기록", 4, purchase_rows),
            self.sheet.prepare_append_range("미등록상품", 6, missing_rows),
            self.sheet.prepare_append_range("전표관리", 3, management_rows),
        ]:
            if prepared:
                updates.append(prepared)
        self.sheet.batch_update_values(updates)

        result.inserted_count = len(purchase_rows)
        result.missing_count = len(missing_rows)
        result.duplicate = "신규 입력"
        if missing_rows:
            reasons = defaultdict(int)
            for row in missing_rows:
                reasons[_extract_reason(str(row[5]))] += 1
            result.details = [f"{reason}: {count}개" for reason, count in reasons.items()]
        return result

    def delete_purchase_records(self, date_text: str, product_name: str = "") -> ProcessResult:
        self.sheet.validate_template()
        target_date = _normalize_date(date_text)
        if not target_date:
            return ProcessResult(status="실패", errors=["삭제할 매입일을 입력해주세요."])

        product_key = normalize_for_match(product_name)
        rows = self.sheet.get_values("'매입기록'!A:D", formatted=True)
        matched_row_numbers: List[int] = []
        for idx, row in enumerate(rows[1:], start=2):
            row_date = _normalize_date(row[0] if len(row) > 0 else "")
            row_product = normalize_for_match(row[1] if len(row) > 1 else "")
            if row_date != target_date:
                continue
            if product_key and row_product != product_key:
                continue
            matched_row_numbers.append(idx)

        # 값만 삭제한다. 행 삭제가 아니며, E:F 수식/서식은 건드리지 않는다.
        # 연속된 행은 하나의 A:D 범위로 묶어 Google Sheets 요청을 최소화한다.
        clear_ranges: List[str] = []
        if matched_row_numbers:
            start = prev = matched_row_numbers[0]
            for row_no in matched_row_numbers[1:]:
                if row_no == prev + 1:
                    prev = row_no
                    continue
                clear_ranges.append(f"'매입기록'!A{start}:D{prev}")
                start = prev = row_no
            clear_ranges.append(f"'매입기록'!A{start}:D{prev}")

        self.sheet.batch_clear_values(clear_ranges)
        return ProcessResult(
            status="성공",
            purchase_date=target_date,
            inserted_count=0,
            message=f"매입기록 {len(matched_row_numbers)}행의 A:D 값만 삭제했습니다.",
            details=[f"상품명: {product_name or '전체'}"],
        )

    def bulk_rename_products(self) -> ProcessResult:
        self.sheet.validate_template()
        mappings = self.sheet.get_values("'상품명변경'!A:B", formatted=True)
        records = self.sheet.get_values("'매입기록'!A:D", formatted=True)

        rename_updates: List[Tuple[str, Any]] = []
        clear_ranges: List[str] = []
        failed: List[str] = []
        changed_count = 0

        for idx, row in enumerate(mappings[1:], start=2):
            old_name = normalize_text(row[0] if len(row) > 0 else "")
            new_name = normalize_text(row[1] if len(row) > 1 else "")
            if not old_name and not new_name:
                continue
            if not old_name or not new_name:
                failed.append(f"{idx}행: 기존상품명/변경상품명 중 빈칸이 있습니다.")
                continue

            old_key = normalize_for_match(old_name)
            matched_rows = []
            for rec_idx, rec in enumerate(records[1:], start=2):
                product = normalize_for_match(rec[1] if len(rec) > 1 else "")
                if product == old_key:
                    matched_rows.append(rec_idx)

            if not matched_rows:
                failed.append(old_name)
                continue

            for row_no in matched_rows:
                rename_updates.append((f"'매입기록'!B{row_no}", new_name))
            clear_ranges.append(f"'상품명변경'!A{idx}:B{idx}")
            changed_count += len(matched_rows)

        self.sheet.update_cells(rename_updates)
        self.sheet.batch_clear_values(clear_ranges)

        result = ProcessResult(
            status="성공" if not failed else "부분성공",
            inserted_count=changed_count,
            message=f"매입기록 상품명 {changed_count}건을 변경했습니다. 성공한 상품명변경 행은 A:B 값만 비웠습니다.",
            errors=failed,
        )
        return result

    def reprocess_missing(self) -> ProcessResult:
        self.sheet.validate_template()
        conversion_rows = self.sheet.read_conversion_rows()
        missing = self.sheet.get_values("'미등록상품'!A:F", formatted=True)
        management = self.sheet.get_values("'전표관리'!A:C", formatted=True)

        aggregations: Dict[Tuple[str, str], Dict[str, Decimal]] = defaultdict(lambda: {"qty": Decimal(0), "amount": Decimal(0)})
        status_updates: List[Tuple[str, Any]] = []
        errors: List[str] = []
        processed_stmt_nos = set()
        processed_missing_row_numbers = set()
        processed_rows = 0

        for idx, row in enumerate(missing[1:], start=2):
            status = normalize_text(row[5] if len(row) > 5 else "")
            if not status or status == "처리완료":
                continue
            stmt_no = normalize_text(row[1] if len(row) > 1 else "")
            purchase_date = _normalize_date(row[2] if len(row) > 2 else "")
            original_name = normalize_text(row[3] if len(row) > 3 else "")
            unit = normalize_text(row[4] if len(row) > 4 else "")
            qty, amount = _parse_pending_status(status)
            if qty is None or amount is None:
                errors.append(f"{idx}행 {original_name}: 수량/금액 정보가 없어 거래명세서 재업로드가 필요합니다.")
                continue

            fake_good = StatementGood(name=original_name, unit=unit, quantity=qty, sum_amount=amount)
            match = self._find_conversion(fake_good, conversion_rows)
            if not match.ok:
                errors.append(f"{idx}행 {original_name}: {_extract_reason(match.reason)}")
                continue

            converted_qty = qty * match.factor
            aggregations[(purchase_date, match.converted_name)]["qty"] += converted_qty
            aggregations[(purchase_date, match.converted_name)]["amount"] += amount
            status_updates.append((f"'미등록상품'!F{idx}", "처리완료"))
            processed_stmt_nos.add(stmt_no)
            processed_missing_row_numbers.add(idx)
            processed_rows += 1

        purchase_rows = [
            [date, product, _sheet_num(data["qty"]), _sheet_num(data["amount"])]
            for (date, product), data in aggregations.items()
        ]
        updates = []
        prepared = self.sheet.prepare_append_range("매입기록", 4, purchase_rows)
        if prepared:
            updates.append(prepared)
        updates.extend([(rng, [[value]]) for rng, value in status_updates])

        # 처리된 전표의 미등록 행이 모두 처리완료가 되었는지 확인해서 전표관리 상태를 갱신한다.
        for stmt_no in processed_stmt_nos:
            still_pending = False
            for row_no, row in enumerate(missing[1:], start=2):
                row_stmt = normalize_text(row[1] if len(row) > 1 else "")
                row_status = normalize_text(row[5] if len(row) > 5 else "")
                if row_stmt != stmt_no:
                    continue
                if row_status == "처리완료" or row_no in processed_missing_row_numbers:
                    continue
                still_pending = True
                break
            new_status = "처리대기" if still_pending else "처리완료"
            for m_idx, m_row in enumerate(management[1:], start=2):
                if normalize_text(m_row[0] if len(m_row) > 0 else "") == stmt_no:
                    updates.append((f"'전표관리'!C{m_idx}", [[new_status]]))
                    break

        self.sheet.batch_update_values(updates)

        return ProcessResult(
            status="성공" if not errors else ("부분성공" if processed_rows else "실패"),
            inserted_count=len(purchase_rows),
            missing_count=max(0, len(errors)),
            message=f"미등록상품 {processed_rows}행을 재처리했습니다.",
            errors=errors,
        )
