# 채소팜 매입단가 전용 프로그램

기존 채소팜 주문대조/재고파악 프로그램과 분리된 **매입단가 전용 웹 프로그램**입니다.  
마켓봄 거래명세서 링크 또는 HTML 파일을 입력하면 `hidden input name="statement"` 값의 JSON을 파싱하고, Google 스프레드시트 템플릿의 정해진 시트/컬럼에 **값만** 입력합니다.

## 핵심 원칙

- 기존 주문대조 프로그램은 건드리지 않습니다.
- Google Sheets 디자인, 서식, 드롭다운, 조건부서식, 열 너비, 셀 병합, 날짜서식, 숫자서식, 원물단가표 수식은 코드에서 만들거나 수정하지 않습니다.
- 템플릿에 필요한 시트/컬럼이 없으면 새로 만들지 않고 오류로 알려줍니다.
- `매입기록`은 `A:D` 컬럼에만 입력합니다.
- `매입기록`의 `E:F` 단가/월 수식은 템플릿이 담당합니다.
- `append_rows` 방식으로 무조건 맨 아래에 붙이지 않고, `A:D` 기준 첫 빈 행을 찾아 `A:D`에만 입력합니다.
- Google Sheets API 429를 줄이기 위해 가능한 한 `batchGet`, `batchUpdate`, `batchClear`로 묶어서 처리합니다.

## 확인한 템플릿 구조

업로드된 `2026_매입단가.xlsx` 기준으로 확인한 시트와 컬럼입니다.

| 시트명 | 컬럼/구조 |
|---|---|
| 매입기록 | 매입일, 상품명, 매입량, 매입총액, 단가, 월 |
| 품목환산표 | 원본품명, 변환품명, 단위, 1개당 환산수량, 사용여부 |
| 원물단가표 | 조회용 화면. 프로그램 수정 없음 |
| 미등록상품 | 업로드일, 전표번호, 매입일, 원본품명, 단위, 처리상태 |
| 상품명변경 | 기존상품명, 변경상품명 |
| 전표관리 | 전표번호, 매입일, 처리상태 |
| 전표상세관리 | 전표번호, 매입일, 원본품명, 변환품명, 단위, 처리상태 |
| 설정 | 드롭다운/조회 보조용. 프로그램 수정 없음 |
| 사용방법 | 설명용. 프로그램 수정 없음 |

## 마켓봄 거래명세서 파싱 기준

HTML 안에서 다음 값을 찾습니다.

```html
<input type="hidden" name="statement" value="...JSON...">
```

그 안의 JSON에서 `goods` 배열을 찾아 아래 값을 읽습니다.

- `name`: 원본품명
- `unit`: 단위
- `quantity`: 수량
- `sum_amount`: 금액

전표번호, 매입일, 총금액은 JSON 안에서 여러 가능한 키 이름을 안정적으로 찾도록 처리했습니다. 전표번호가 JSON에서 안 보이고 URL 처리인 경우에는 URL 마지막 값을 보조 전표번호로 사용합니다.

## 처리 흐름

1. 마켓봄 거래명세서 링크 입력 또는 HTML 파일 업로드
2. HTML의 `statement` JSON 파싱
3. `goods` 목록에서 원본품명, 단위, 수량, 금액 추출
4. `품목환산표` 기준으로 변환품명과 환산수량 계산
5. 같은 변환품명은 전표 1건 안에서 합산
6. `매입기록` A:D에만 입력
7. 미등록/단위불일치/환산수량 없음/변환품명 없음은 `미등록상품`에 기록
8. `전표관리`로 중복 업로드 방지
9. `전표상세관리`에 품목별 처리상태 기록

## 품목환산표 매칭 기준

- 거래명세서 `name`과 `품목환산표.원본품명`이 같아야 합니다.
- `품목환산표.단위`가 비어 있으면 단위 상관없이 매칭합니다.
- `품목환산표.단위`가 적혀 있으면 거래명세서 단위와 같을 때만 매칭합니다.
- `1개당 환산수량`은 필수입니다.
- `1`, `1.0`, `1.000`, `0.5` 같은 값은 숫자로 읽습니다.
- `사용여부`가 `사용`이면 매입기록에 반영합니다.
- `사용여부`가 `미사용`이면 매입기록과 미등록상품에는 넣지 않고, 전표상세관리에는 `미사용`으로 기록합니다.

## 미등록상품 재처리 방식

현재 템플릿의 `미등록상품` 시트에는 수량/금액 전용 컬럼이 없습니다.  
그래서 프로그램이 생성한 미등록상품 행은 재처리가 가능하도록 `처리상태`에 다음처럼 최소 정보를 함께 저장합니다.

```text
처리대기 | 품목환산표 미등록 | 수량=10 | 금액=100000
```

재처리 버튼은 `처리완료`가 아닌 행을 다시 확인합니다.

- 품목환산표에 등록되면 매입기록에 입력합니다.
- 해당 미등록상품 행의 처리상태를 `처리완료`로 바꿉니다.
- 전표상세관리의 변환품명/처리상태를 갱신합니다.
- 해당 전표의 미등록 행이 모두 완료되면 전표관리 상태도 `처리완료`로 바꿉니다.
- 수량/금액 정보가 없는 예전 행은 거래명세서 재업로드가 필요하다고 안내합니다.

## 매입기록 삭제 방식

삭제 기능은 서식과 수식을 보호하기 위해 행 자체를 삭제하지 않고, `매입기록` 시트의 `A:D` 값만 비웁니다.

- 날짜만 입력: 해당 날짜의 매입기록 A:D 값 전체 삭제
- 날짜 + 상품명 입력: 해당 날짜의 해당 상품 A:D 값만 삭제
- 전표관리/전표상세관리는 자동 삭제하지 않습니다.

## 상품명 일괄변경 방식

`상품명변경` 시트에 입력된 값을 기준으로 `매입기록` 시트의 상품명만 변경합니다.

- `기존상품명`을 `변경상품명`으로 변경
- 성공한 상품명변경 행은 A:B 값만 비웁니다.
- 못 찾은 상품명은 화면에 안내합니다.
- 시트 컬럼 추가, 서식 변경은 하지 않습니다.

## 환경변수

Render > Environment에서 아래 값을 설정하세요.

### 필수

| 환경변수 | 설명 |
|---|---|
| `PURCHASE_SPREADSHEET_ID` | 매입단가 Google 스프레드시트 ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google 서비스 계정 JSON 전체 내용 |

### 선택

| 환경변수 | 설명 |
|---|---|
| `FLASK_SECRET_KEY` | Flask 세션용 임의 문자열 |
| `GOOGLE_APPLICATION_CREDENTIALS` | 로컬 실행 시 서비스 계정 JSON 파일 경로. Render에서는 보통 `GOOGLE_SERVICE_ACCOUNT_JSON` 권장 |

## Google 스프레드시트 준비 방법

1. `2026_매입단가.xlsx`를 Google Drive에 업로드합니다.
2. Google 스프레드시트로 엽니다.
3. URL에서 스프레드시트 ID를 복사합니다.
   - 예: `https://docs.google.com/spreadsheets/d/스프레드시트ID/edit`
4. 서비스 계정 이메일을 해당 스프레드시트에 편집자로 공유합니다.
5. Render 환경변수 `PURCHASE_SPREADSHEET_ID`에 스프레드시트 ID를 넣습니다.
6. Render 환경변수 `GOOGLE_SERVICE_ACCOUNT_JSON`에 서비스 계정 JSON 전체를 넣습니다.

## Render 배포 방법

### 방법 1. GitHub 연결

1. 이 ZIP의 압축을 풉니다.
2. 전체 파일을 새 GitHub 저장소에 업로드합니다.
3. Render에서 New > Web Service를 선택합니다.
4. 해당 GitHub 저장소를 연결합니다.
5. 아래 설정을 사용합니다.

```text
Environment: Python
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
```

6. 환경변수를 설정하고 Deploy를 실행합니다.

### 방법 2. render.yaml 사용

`render.yaml`이 포함되어 있으므로 Render Blueprint로 배포할 수 있습니다.  
배포 후 `PURCHASE_SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`은 Render 대시보드에서 직접 입력하세요.

## 로컬 실행 방법

```bash
python -m venv .venv
source .venv/bin/activate  # Windows는 .venv\\Scripts\\activate
pip install -r requirements.txt
export PURCHASE_SPREADSHEET_ID="구글시트ID"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
python app.py
```

브라우저에서 아래 주소를 엽니다.

```text
http://localhost:8000
```

## 파일 구성

```text
purchase_price_app/
├── app.py
├── requirements.txt
├── runtime.txt
├── Procfile
├── render.yaml
├── README.md
├── templates/
│   └── index.html
└── services/
    ├── __init__.py
    ├── config.py
    ├── marketbom_parser.py
    ├── processor.py
    └── sheets.py
```

## 주의사항

- 이 프로그램은 템플릿을 새로 만들거나 디자인을 보정하지 않습니다.
- 필요한 시트/컬럼이 없으면 오류로 멈춥니다.
- 원물단가표, 설정, 사용방법 시트는 수정하지 않습니다.
- 링크 처리와 HTML 업로드 처리는 같은 파서를 사용하므로 결과가 같아야 합니다.
- 공개 링크에서 HTML 안에 `statement` 값이 내려오지 않는 경우에는 HTML 파일 업로드 방식으로 처리하세요.
