"""Matches chat answers to the SANC catalog/churches and builds the POST /api/orders body.

The body mirrors the admin 제품주문서 (fe OrderListInput.tsx) so admin, print and factory
screens read Kakao orders the same way. New orderers go through ordererProfile and
OrdersService finds-or-creates the member by phone.
"""
import base64
import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation

from orders import KST, STORES, OrderError

UNIT = re.compile(r'[（(]\s*(박스|개|병|통|팩|봉|세트)\s*[）)]')
DIRECTOR_TYPES = {'GWANJANG', 'CHONGMU'}


def norm(name):
    name = (name or '').lower().strip().lstrip('*').strip()
    name = UNIT.sub('', name).replace('리터', 'l').replace('밀리', 'm')
    return re.sub(r'[\s()（）]', '', name)


def unit_of(product):
    units = set(UNIT.findall((product.get('productName') or '') + ' ' + str(product.get('spec') or '')))
    return units.pop() if len(units) == 1 else None


def match_products(catalog, item):
    """Catalog rows for a chat item. Exact name first, then names containing it.

    The unit must equal the product's (박스)/(개)… annotation; there is no box↔ea conversion.
    선물세트 needs a 인사장 and stays in the app.
    """
    wanted = norm(item['name'])
    rows = [p for p in catalog if p.get('productName') and p.get('openStock') is not False
            and p.get('category') != '선물세트' and unit_of(p) == item['unit']]
    exact = [p for p in rows if norm(p['productName']) == wanted]
    found = exact or ([p for p in rows if wanted in norm(p['productName'])] if len(wanted) >= 2 else [])
    names = {}
    for p in found:
        names.setdefault(p['productName'].strip(), p)
    return list(names.values())


def price_of(product, field='wholesalePrice'):
    try:
        price = Decimal(str(product[field]))
        if not price.is_finite() or price < 0:
            raise ValueError()
        return price
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise OrderError(f"상품 단가가 등록되지 않았습니다: {product.get('productName')}") from None


def search_churches(churches, query):
    """Same rules as the order form's 중앙 search: digits → branchCode, else substring."""
    q = (query or '').strip()
    if not q:
        return []
    exact = [c for c in churches if c.get('name') == q]
    if exact:
        return exact
    if q.isdigit():
        return [c for c in churches if str(c.get('branchCode') or '') == q][:30]
    q = q.lower()
    return [c for c in churches
            if q in ' '.join(str(c.get(k) or '') for k in ('name', 'region', 'branchCode', 'assigner')).lower()][:30]


def money(value):
    return f'{value:,.0f}원'


def display_orderer(orderer):
    name = orderer['name'].strip()
    if orderer.get('memberType') in DIRECTOR_TYPES:
        return name if name.endswith('관') else name + '관'
    return name[:-1] if name.endswith('관') else name


def window(date, kind, time):
    if kind == 'parcel':
        return f'{date}T09:00:00.000Z'
    hour, minute = map(int, time['time'].split(':'))
    if time['ampm'] == '오전':
        hour = 0 if hour == 12 else hour
    elif hour != 12:
        hour += 12
    # The order form stores the wall-clock time with a Z suffix; keep the same convention.
    return f'{date}T{hour:02d}:{minute:02d}:00.000Z'


def line_shipments(s, sender):
    r = s['recipient']
    delivery = s['kind'] == 'delivery'
    ship = {'kind': s['kind'], 'companyName': s['company'],
            'deliveryDate': s['date'] if delivery else '', 'deliveryAmPm': s['time']['ampm'] if delivery else '',
            'deliveryTime': s['time']['time'] if delivery else '',
            'recipientName': r['name'], 'recipientPhone': r['phone'], 'recipientAddress': r['address'],
            'recipientAddressDetail': '', 'parcelShipDate': '' if delivery else s['date'],
            'senderName': '' if delivery else sender['name'], 'senderPhone': '' if delivery else sender['phone'],
            'senderAddress': '' if delivery else sender['address'], 'senderAddressDetail': '',
            'sameAsSenderAddress': False, 'contactMode': 'address'}
    lines = [{'product': i['productName'], 'qty': i['quantity'], 'baseQty': i['quantity'], 'note': '',
              'unitPrice': i['price'], 'lineSection': 'box', 'deliveryOnly': False, 'ship': ship}
             for i in s['items']]
    raw = json.dumps({'v': 1, 'lines': lines}, ensure_ascii=False, separators=(',', ':')).encode()
    return '배송상세:' + base64.urlsafe_b64encode(raw).decode().rstrip('=')


def build_order(s, admin_id, today=None):
    """Return (request body, preview text) from a completed dialog state."""
    today = today or dt.datetime.now(KST).date()
    o, r = s['orderer'], s['recipient']
    branch = STORES[s['store']]
    delivery = s['kind'] == 'delivery'
    kind_label = '배달' if delivery else '택배'
    # 택배 기표지의 보내는 분 = 주문자. Members have no address here, so the recipient's is used.
    sender = {'name': display_orderer(o), 'phone': o['phone'], 'address': r['address']}
    total = sum(Decimal(str(i['price'])) * i['quantity'] for i in s['items'])
    notes = [f"주문자:{display_orderer(o)}", f"연락처:{o['phone']}", f'주문일자:{today.isoformat()}',
             f"중앙:{o['churchName']}"]
    if delivery:
        notes += [f"배달업체명:{s['company']}", f"배달일:{s['date']} {s['time']['ampm']} {s['time']['time']}",
                  f"받는분:{r['name']} / {r['phone']} / {r['address'] or '-'}"]
    else:
        notes += [f"택배업체명:{s['company']}", f"택배발송일:{s['date']}",
                  f"보내는사람:{sender['name']} / {sender['phone']} / {sender['address']}"]
        if r['name']:
            notes.append(f"받는분성함:{r['name']}")
    notes += ['수취연락:주소', f"받는분주소:{r['address']}", f'주문작업지역:{branch}', f'지부매장:{branch}',
              '인사장종류:없음']
    notes += [f"[{kind_label}] {i['productName']} {i['quantity']}개" for i in s['items']]
    notes.append(line_shipments(s, sender))
    body = {'userId': o['userId'] if o.get('userId') else admin_id, 'status': 'PLACED',
            'totalAmount': float(total), 'notes': ' / '.join(notes), 'extraNote': '',
            'items': [{'productName': i['productName'], 'quantity': i['quantity'], 'price': i['price']}
                      for i in s['items']],
            'shipment': {'fulfillmentType': 'PARCEL', 'carrier': s['company'],
                         'deliveryAddress': r['address'] if delivery else sender['address'],
                         'estimatedWindow': window(s['date'], s['kind'], s.get('time'))}}
    if not o.get('userId'):
        # OrdersService reuses the member with this phone, or registers a new one.
        body['ordererProfile'] = {'fullname': o['name'], 'phone': o['phone'], 'churchId': o['churchId']}
    member = ('기존 회원' if o.get('userId')
              else '신규 회원으로 등록됩니다. 같은 번호의 회원이 있으면 그 회원으로 접수됩니다.')
    when = f"{s['date']} {s['time']['ampm']} {s['time']['time']}" if delivery else f"발송일 {s['date']}"
    preview = '\n'.join(
        ['── 주문 확인 ──', f'매장: {branch}', f"주문자: {o['name']} {o['phone']}", f'  → {member}',
         f"중앙: {o['churchName']}", f"업체: {s['company']}", f'배송: {kind_label}, {when}',
         f"받는분: {r['name']} {r['phone']}", f"주소: {r['address']}"] +
        [f"{i['productName']} × {i['quantity']} = {money(Decimal(str(i['price'])) * i['quantity'])}"
         for i in s['items']] +
        [f'상품 합계: {money(total)}'])
    return body, preview
