"""Conservative channel parser. No Windows, filesystem writes or network calls."""
import datetime as dt
import re
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


class OrderError(ValueError):
    pass


DATE = re.compile(r'(?:(\d{4})[년/.-]\s*)?(\d{1,2})[월/.-]\s*(\d{1,2})일?(?:\s*(?:납품|배송|발송))?(?:\s*(?:까지|에))?')
ITEM = re.compile(r'(.+?)\s+(\d+)\s*(박스|병|통|개|팩|봉|세트|box|ea)', re.I)
# Courtesy endings such as "주문합니다.(테스트)"; never strips words like 취소 that change meaning.
TRAILING = re.compile(r'\s*(?:주문\s*(?:합니다|할게요|해요|해\s*주세요|드립니다|부탁(?:드립니다|해요)?|이요|요)'
                      r'|부탁(?:드립니다|해요)|입니다|[(（]\s*테스트\s*[)）]|[.!~。])\s*$')


def strip_trailing(part):
    while True:
        stripped = TRAILING.sub('', part).strip()
        if stripped == part:
            return part
        part = stripped


def parse_order(text, today=None):
    today = today or dt.datetime.now(KST).date()
    parts = [strip_trailing(p.strip()) for p in re.split(r'[,、|\n]+', text) if p.strip()]
    parts = [p for p in parts if p]
    if len(parts) < 3:
        raise OrderError('납품처, 날짜, 상품과 수량을 쉼표로 구분해 입력해 주세요. 예: 신선유통, 10월 21일, 1급진간장 1.8리터 300박스')
    # Classify each part by shape so the order of 납품처/날짜/상품 does not matter.
    dates = [m for m in map(DATE.fullmatch, parts) if m]
    lines = [p for p in parts if not DATE.fullmatch(p) and ITEM.fullmatch(p)]
    others = [p for p in parts if not DATE.fullmatch(p) and not ITEM.fullmatch(p)]
    if len(dates) != 1:
        raise OrderError('날짜는 2026-10-21, 10월 21일 또는 10/21 형식으로 한 번만 입력해 주세요.')
    if not lines:
        raise OrderError('상품명 뒤에 수량과 단위를 써 주세요. 예: 1급진간장 1.8리터 300박스')
    if len(others) != 1:
        raise OrderError('납품처는 한 곳만, 상품은 “상품명 수량단위” 형식으로 입력해 주세요. 예: 신선유통, 10월 21일, 1급진간장 1.8리터 300박스')
    company = others[0]
    if len(company) > 100 or len(lines) > 20:
        raise OrderError('납품처 이름 또는 상품 줄이 너무 깁니다. 상품은 최대 20줄입니다.')
    year, month, day = dates[0].groups()
    try:
        date = dt.date(int(year or today.year), int(month), int(day))
    except ValueError:
        raise OrderError('존재하지 않는 날짜입니다.') from None
    if date < today:
        raise OrderError('지난 날짜입니다. 다음 해 주문이라면 연도까지 입력해 주세요.')
    items = []
    for line in lines:
        name, qty, unit = ITEM.fullmatch(line).groups()
        qty = int(qty)
        if not 1 <= qty <= 100000:
            raise OrderError('수량은 1~100000 사이 정수로 입력해 주세요.')
        unit = {'box': '박스', 'ea': '개'}.get(unit.lower(), unit)
        if any(i['name'] == name and i['unit'] == unit for i in items):
            raise OrderError('동일 상품이 반복되었습니다. 수량을 합쳐 한 줄로 입력해 주세요.')
        items.append({'name': name, 'qty': qty, 'unit': unit})
    return {'company': company, 'ship_date': date.isoformat(), 'items': items}


def summary(order):
    return '\n'.join([f"납품처: {order['company']}", f"납품 요청일: {order['ship_date']}"] +
                     [f"{i['name']} × {i['qty']}{i['unit']}" for i in order['items']])

