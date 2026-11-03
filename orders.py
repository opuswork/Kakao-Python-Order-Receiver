"""Order text extraction. No Windows, filesystem writes or network calls.

A message may carry any subset of the order form (매장, 주문자, 날짜, 상품, 업체, 배송 방법…)
in any order, labelled ("주문자 성명: 이용식") or not. Missing values are asked by dialog.py.
"""
import datetime as dt
import re
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')

# Order form branch tabs → notes label read by OrdersService.parseStoreRegionFromNotes.
STORES = {'NAMBU': '남부(기장)', 'JUNGBU': '중부(덕소)', 'SEOBU': '서부(소사)'}
STORE_WORDS = {'남부': 'NAMBU', '중부': 'JUNGBU', '서부': 'SEOBU'}
KINDS = {'택배': 'parcel', '배달': 'delivery'}


class OrderError(ValueError):
    pass


DATE_SUFFIX = r'(?:\s*(?:납품|배송|발송|배달|도착))?(?:\s*(?:까지|에|요청))?(?:\s*(?:납품|배송|발송|배달))?'
DATE_FULL = re.compile(r'(?:(\d{4})[년/.-]\s*)?(\d{1,2})[월/.-]\s*(\d{1,2})일?' + DATE_SUFFIX)
DATE_DAY = re.compile(r'(\d{1,2})일' + DATE_SUFFIX)
ITEM = re.compile(r'(.+?)\s+(\d+)\s*(박스|병|통|개|팩|봉|세트|box|ea)', re.I)
PHONE = re.compile(r'(01[016789])[-\s.]?(\d{3,4})[-\s.]?(\d{4})')
TIME = re.compile(r'(오전|오후|새벽|아침|저녁)?\s*(\d{1,2})(?:\s*시(?:\s*(\d{1,2})\s*분|\s*반)?|:(\d{2}))(?:\s*(?:까지|에|도착))?')
NAME = re.compile(r'[가-힣A-Za-z][가-힣A-Za-z ]{0,18}[가-힣A-Za-z]')
LABEL = re.compile(r'([^:：]{1,12}?)\s*[:：]\s*(.*)')
# Courtesy endings such as "주문합니다.(테스트)"; never strips words like 취소 that change meaning.
TRAILING = re.compile(r'\s*(?:주문\s*(?:합니다|할게요|해요|해\s*주세요|드립니다|부탁(?:드립니다|해요)?|이요|요)'
                      r'|부탁(?:드립니다|해요)|입니다|[(（]\s*테스트\s*[)）]|[.!~。])\s*$')

LABELS = {
    'orderer': {'주문자', '주문자성명', '주문자이름', '성명', '이름', '주문회원'},
    'phone': {'전화번호', '연락처', '주문자연락처', '주문자전화번호', '전화', '휴대폰'},
    'time': {'배달시간', '시간', '도착시간'},
    'store': {'주문매장', '매장', '지역', '주문작업지역', '지부매장'},
    'date': {'납품일', '날짜', '배송일', '배달일', '발송일', '택배발송일', '납품요청일'},
    'company': {'업체', '업체명', '거래처', '납품처', '받는곳', '배달업체명', '택배업체명'},
    'recipient': {'받는분', '받는사람', '받는분성함', '수령인'},
    'recipient_phone': {'받는분연락처', '받는분전화번호', '수령인연락처'},
    'address': {'주소', '상세주소', '받는분주소', '받는분상세주소', '배송지'},
    'church': {'중앙'},
    'kind': {'배송', '배송방법', '배송선택'},
}
LABEL_OF = {word: key for key, words in LABELS.items() for word in words}


def strip_trailing(part):
    while True:
        stripped = TRAILING.sub('', part).strip()
        if stripped == part:
            return part
        part = stripped


def clean(value):
    # OrdersService splits notes on " / "; never let user text add a field separator.
    return re.sub(r'\s+', ' ', re.sub(r'[/\r\n]', ' ', value or '')).strip()


def format_phone(value):
    m = PHONE.search(value or '')
    if not m:
        raise OrderError('전화번호는 010-1234-5678 형식으로 입력해 주세요.')
    return '-'.join(m.groups())


def parse_date(text, today):
    m = DATE_FULL.fullmatch(text)
    if m:
        year, month, day = m.groups()
    else:
        m = DATE_DAY.fullmatch(text)
        if not m:
            return None
        year, month, day = None, today.month, m.group(1)
        # "27일납품": the next 27th, this month or the next.
        if int(day) < today.day:
            month = today.month % 12 + 1
            year = today.year + (today.month == 12)
    try:
        date = dt.date(int(year or today.year), int(month), int(day))
    except ValueError:
        raise OrderError('존재하지 않는 날짜입니다.') from None
    if date < today:
        raise OrderError('지난 날짜입니다. 다음 해 주문이라면 연도까지 입력해 주세요.')
    return date.isoformat()


def parse_time(text):
    m = TIME.fullmatch(text.strip())
    if not m:
        return None
    word, hour, minute, colon_minute = m.groups()
    hour = int(hour)
    minute = int(minute or colon_minute or (30 if '반' in text else 0))
    if minute > 59 or hour > 23:
        raise OrderError('배달 시간을 확인해 주세요. 예: 오전 10시, 14:30')
    if word in ('오후', '저녁') and hour < 12:
        hour += 12
    if word in ('오전', '새벽', '아침') and hour == 12:
        hour = 0
    ampm = '오전' if hour < 12 else '오후'
    hour12 = hour % 12 or 12
    # Same "오전|오후 HH:MM" pair the order form stores in 배달일.
    return {'ampm': ampm, 'time': f'{hour12:02d}:{minute:02d}'}


def parse_item(text):
    m = ITEM.fullmatch(text)
    if not m:
        return None
    name, qty, unit = m.groups()
    qty = int(qty)
    if not 1 <= qty <= 100000:
        raise OrderError('수량은 1~100000 사이 정수로 입력해 주세요.')
    return {'name': name.strip(), 'qty': qty, 'unit': {'box': '박스', 'ea': '개'}.get(unit.lower(), unit)}


def parse_person(text):
    """"홍길동 010-1234-5678 서울 종로구 …" → name, phone and the rest (address)."""
    m = PHONE.search(text)
    if not m:
        return None
    name = clean(text[:m.start()]).rstrip(',:')
    rest = clean(text[m.end():]).lstrip(',: ')
    return {'name': name, 'phone': '-'.join(m.groups()), 'rest': rest}


def valid_name(name):
    return bool(name) and NAME.fullmatch(name) is not None


def store_of(text):
    words = [code for word, code in STORE_WORDS.items() if word in text]
    return words[0] if len(words) == 1 and len(text) <= 12 else None


def kind_of(text):
    words = [code for word, code in KINDS.items() if word in text]
    return words[0] if len(words) == 1 and len(text) <= 8 else None


def split_parts(text):
    # " / " separates fields in many chat orders; "11/10" (no spaces) stays a date.
    return [p for p in (strip_trailing(p.strip()) for p in re.split(r'[,、|\n]+|\s+/\s+', text)) if p]


def extract(text, today=None):
    """Return every order field found in text. Unclassified parts are listed in 'unknown'."""
    today = today or dt.datetime.now(KST).date()
    found = {'items': [], 'unknown': []}
    address = []

    def put(key, value):
        if key in found and found[key] != value:
            raise OrderError(f'{key_label(key)} 값이 두 번 입력되었습니다. 한 번만 적어 주세요.')
        found[key] = value

    for part in split_parts(text):
        label = LABEL.fullmatch(part)
        key = LABEL_OF.get(re.sub(r'\s+', '', label.group(1))) if label else None
        value = label.group(2).strip() if key else part
        if key and not value:
            continue
        if key == 'orderer':
            person = parse_person(value)
            if person:
                put('orderer_phone', person['phone'])
                value = person['name']
            put('orderer_name', clean(value))
        elif key == 'phone':
            put('orderer_phone', format_phone(value))
        elif key == 'time':
            parsed = parse_time(value)
            if not parsed:
                raise OrderError('배달 시간을 확인해 주세요. 예: 오전 10시, 14:30')
            put('time', parsed)
        elif key == 'store':
            code = store_of(value)
            if not code:
                raise OrderError('매장은 남부, 중부, 서부 중 하나로 적어 주세요.')
            put('store', code)
        elif key == 'date':
            date = parse_date(value, today)
            if not date:
                raise OrderError('날짜는 11월 10일, 11/10 또는 2026-11-10 형식으로 입력해 주세요.')
            put('date', date)
        elif key == 'company':
            put('company', clean(value))
        elif key == 'recipient':
            person = parse_person(value)
            if person:
                put('recipient_phone', person['phone'])
                if person['rest']:
                    address.append(person['rest'])
                value = person['name']
            put('recipient_name', clean(value))
        elif key == 'recipient_phone':
            put('recipient_phone', format_phone(value))
        elif key == 'address':
            address.append(clean(value))
        elif key == 'church':
            put('church', clean(value))
        elif key == 'kind':
            code = kind_of(value)
            if not code:
                raise OrderError('배송 방법은 택배 또는 배달로 적어 주세요.')
            put('kind', code)
        elif (date := parse_date(part, today)):
            put('date', date)
        elif (item := parse_item(part)):
            if any(i['name'] == item['name'] and i['unit'] == item['unit'] for i in found['items']):
                raise OrderError('동일 상품이 반복되었습니다. 수량을 합쳐 한 줄로 입력해 주세요.')
            found['items'].append(item)
        elif (code := store_of(part)):
            put('store', code)
        elif (code := kind_of(part)):
            put('kind', code)
        elif (parsed := parse_time(part)):
            put('time', parsed)
        elif (person := parse_person(part)) and valid_name(person['name']) and not person['rest']:
            put('orderer_name', person['name'])
            put('orderer_phone', person['phone'])
        else:
            found['unknown'].append(clean(part))
    if address:
        found['address'] = ' '.join(address)
    if len(found['items']) > 20:
        raise OrderError('상품은 한 번에 최대 20줄까지 주문할 수 있습니다.')
    return found


def key_label(key):
    return {'orderer_name': '주문자', 'orderer_phone': '전화번호', 'time': '배달 시간', 'store': '매장',
            'date': '날짜', 'company': '업체', 'recipient_name': '받는분', 'recipient_phone': '받는분 연락처',
            'church': '중앙', 'kind': '배송 방법'}.get(key, key)
