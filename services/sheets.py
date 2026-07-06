from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import build_credentials


class TemplateValidationError(RuntimeError):
    pass


class SheetOperationError(RuntimeError):
    pass


REQUIRED_SHEETS: Dict[str, List[str]] = {
    "매입기록": ["매입일", "상품명", "매입량", "매입총액", "단가", "월"],
    "품목환산표": ["원본품명", "변환품명", "단위", "1개당 환산수량", "사용여부"],
    "미등록상품": ["업로드일", "전표번호", "매입일", "원본품명", "단위", "처리상태"],
    "상품명변경": ["기존상품명", "변경상품명"],
    "전표관리": ["전표번호", "매입일", "처리상태"],
    "전표상세관리": ["전표번호", "매입일", "원본품명", "변환품명", "단위", "처리상태"],
    # 아래 3개는 프로그램이 수정하지 않지만 템플릿 존재 여부를 확인한다.
    "원물단가표": [],
    "설정": [],
    "사용방법": [],
}


@dataclass(frozen=True)
class ConversionRow:
    original_name: str
    converted_name: str
    unit: str
    factor: Optional[Any]
    active: bool
    row_number: int


def col_to_a1(col: int) -> str:
    """1-based column number to A1 column string."""
    result = ""
    while col:
        col, remainder = divmod(col - 1, 26)
        result = chr(65 + remainder) + result
    return result


def is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


class PurchaseSheetClient:
    """Google Sheets values-only client.

    이 클래스는 spreadsheets.values.* API와 값 clear만 사용한다.
    서식, 조건부서식, 드롭다운, 열 너비, 병합, 수식 영역은 만들거나 수정하지 않는다.
    """

    def __init__(self, spreadsheet_id: str, credentials_info: Dict[str, Any]):
        self.spreadsheet_id = spreadsheet_id
        credentials = build_credentials(credentials_info)
        self.service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self._sheet_id_by_name: Dict[str, int] = {}

    def _execute(self, request):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 429:
                raise SheetOperationError("Google Sheets API 429 오류입니다. 잠시 후 다시 실행해주세요.") from exc
            raise SheetOperationError(f"Google Sheets API 오류: {exc}") from exc

    def load_metadata(self) -> Dict[str, int]:
        meta = self._execute(
            self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets(properties(sheetId,title))",
            )
        )
        self._sheet_id_by_name = {
            s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])
        }
        return self._sheet_id_by_name

    def validate_template(self) -> None:
        sheet_ids = self.load_metadata()
        missing_sheets = [name for name in REQUIRED_SHEETS if name not in sheet_ids]
        if missing_sheets:
            raise TemplateValidationError("템플릿에 필요한 시트가 없습니다: " + ", ".join(missing_sheets))

        # 한 번의 batchGet으로 헤더만 확인한다. 원물단가표/설정/사용방법은 존재만 확인한다.
        ranges = []
        check_names = []
        for sheet_name, headers in REQUIRED_SHEETS.items():
            if not headers:
                continue
            end_col = col_to_a1(len(headers))
            ranges.append(f"'{sheet_name}'!A1:{end_col}1")
            check_names.append(sheet_name)
        result = self._execute(
            self.service.spreadsheets().values().batchGet(
                spreadsheetId=self.spreadsheet_id,
                ranges=ranges,
                majorDimension="ROWS",
                valueRenderOption="UNFORMATTED_VALUE",
            )
        )
        by_range = result.get("valueRanges", [])
        for sheet_name, vr in zip(check_names, by_range):
            actual = [normalize_cell(x) for x in (vr.get("values", [[]])[0] if vr.get("values") else [])]
            expected = REQUIRED_SHEETS[sheet_name]
            for idx, header in enumerate(expected):
                got = actual[idx] if idx < len(actual) else ""
                if got != header:
                    raise TemplateValidationError(f'{sheet_name} 시트에 “{header}” 컬럼이 없습니다.')

    def get_values(self, range_name: str, formatted: bool = False) -> List[List[Any]]:
        value_render = "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE"
        result = self._execute(
            self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=range_name,
                majorDimension="ROWS",
                valueRenderOption=value_render,
            )
        )
        return result.get("values", [])

    def batch_get_values(self, ranges: Sequence[str], formatted: bool = False) -> Dict[str, List[List[Any]]]:
        if not ranges:
            return {}
        value_render = "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE"
        result = self._execute(
            self.service.spreadsheets().values().batchGet(
                spreadsheetId=self.spreadsheet_id,
                ranges=list(ranges),
                majorDimension="ROWS",
                valueRenderOption=value_render,
            )
        )
        return {vr.get("range", ranges[i]): vr.get("values", []) for i, vr in enumerate(result.get("valueRanges", []))}

    def batch_update_values(self, updates: Sequence[Tuple[str, List[List[Any]]]]) -> None:
        data = []
        for range_name, values in updates:
            if values:
                data.append({"range": range_name, "majorDimension": "ROWS", "values": values})
        if not data:
            return
        self._execute(
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
        )

    def batch_clear_values(self, ranges: Sequence[str]) -> None:
        if not ranges:
            return
        self._execute(
            self.service.spreadsheets().values().batchClear(
                spreadsheetId=self.spreadsheet_id,
                body={"ranges": list(ranges)},
            )
        )

    def _first_blank_row(self, values: List[List[Any]], width: int, start_row: int = 2) -> int:
        # values는 A1부터 시작한다. 요청대로 지정 컬럼 기준 첫 빈 행을 찾는다.
        row_number = start_row
        for idx in range(start_row - 1, len(values)):
            row = values[idx]
            relevant = [(row[i] if i < len(row) else "") for i in range(width)]
            if all(is_blank(v) for v in relevant):
                return idx + 1
            row_number = idx + 2
        return row_number

    def append_values_to_first_blank(self, sheet_name: str, width: int, rows: List[List[Any]]) -> int:
        if not rows:
            return 0
        end_col = col_to_a1(width)
        existing = self.get_values(f"'{sheet_name}'!A:{end_col}")
        start_row = self._first_blank_row(existing, width=width, start_row=2)
        target = f"'{sheet_name}'!A{start_row}:{end_col}{start_row + len(rows) - 1}"
        self.batch_update_values([(target, rows)])
        return len(rows)

    def prepare_append_range(self, sheet_name: str, width: int, rows: List[List[Any]]) -> Optional[Tuple[str, List[List[Any]]]]:
        if not rows:
            return None
        end_col = col_to_a1(width)
        existing = self.get_values(f"'{sheet_name}'!A:{end_col}")
        start_row = self._first_blank_row(existing, width=width, start_row=2)
        return (f"'{sheet_name}'!A{start_row}:{end_col}{start_row + len(rows) - 1}", rows)

    def read_conversion_rows(self) -> List[ConversionRow]:
        values = self.get_values("'품목환산표'!A:E")
        rows: List[ConversionRow] = []
        for idx, row in enumerate(values[1:], start=2):
            original = normalize_cell(row[0] if len(row) > 0 else "")
            converted = normalize_cell(row[1] if len(row) > 1 else "")
            unit = normalize_cell(row[2] if len(row) > 2 else "")
            factor = row[3] if len(row) > 3 else ""
            status = normalize_cell(row[4] if len(row) > 4 else "")
            if not any([original, converted, unit, factor, status]):
                continue
            rows.append(
                ConversionRow(
                    original_name=original,
                    converted_name=converted,
                    unit=unit,
                    factor=factor,
                    active=(status == "사용"),
                    row_number=idx,
                )
            )
        return rows

    def update_cells(self, updates: Sequence[Tuple[str, Any]]) -> None:
        payload = [(rng, [[value]]) for rng, value in updates]
        self.batch_update_values(payload)
