"""Conservative channel parser. No Windows, filesystem writes or network calls."""
import datetime as dt
import re
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


class OrderError(ValueError):
    pass


def parse_order(text, today=None):
    today = today or dt.datetime.now(KST).date()
    parts = [p.strip() for p in re.split(r'[,、|\n]+', text) if p.strip()]
    if len(parts) < 3:
        raise OrderError('납품처, 날짜, 상품과 수량 순서로 입력해 주세요. 예: 신선유통, 10월 21일, 1급진간장 1.8리터 300박스')
    company, date_text, *lines = parts
    if len(company) > 100 or len(lines) > 20:
        raise OrderError('납품처 이름 또는 상품 줄이 너무 깁니다. 상품은 최대 20줄입니다.')
    match = re.fullmatch(r'(?:(\d{4})[년/.-]\s*)?(\d{1,2})[월/.-]\s*(\d{1,2})일?(?:\s*(?:납품|배송|발송))?', date_text)
    if not match:
        raise OrderError('날짜는 2026-10-21, 10월 21일 또는 10/21 형식으로 입력해 주세요.')
    year, month, day = match.groups()
    try:
        date = dt.date(int(year or today.year), int(month), int(day))
    except ValueError:
        raise OrderError('존재하지 않는 날짜입니다.') from None
    if date < today:
        raise OrderError('지난 날짜입니다. 다음 해 주문이라면 연도까지 입력해 주세요.')
    items = []
    for line in lines:
        m = re.fullmatch(r'(.+?)\s+(\d+)\s*(박스|병|통|개|팩|봉|세트|box|ea)', line, re.I)
        if not m:
            raise OrderError('상품명 뒤에 수량과 단위를 써 주세요. 상품이 여러 개면 줄바꿈해 주세요. 예: 1급진간장 1.8리터 300박스')
        name, qty, unit = m.groups()
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

