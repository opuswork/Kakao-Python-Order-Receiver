"""Adapter for the supplied Prisma models and POST /api/orders example.

Partners are read from the supplied API and filtered by an explicit allowlist. Order writes always go through the existing API.
"""
import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from orders import OrderError, summary

PRICE_FIELDS = {'wholesalePrice', 'associatePrice', 'priceOver100man',
                'priceOver500man', 'retailPrice', 'supermarketPrice', 'schoolServePrice'}


def positive_id(value):
    return type(value) is int and value > 0


def select_partner(parsed, binding, partners):
    # Partner.userId records the creator; the supplied service deliberately shares search.
    matches = [p for p in partners if positive_id(p.get('id'))
               and p['id'] in binding.get('allowed_partner_ids', [])
               and parsed['company'] in [p.get('name'), *p.get('aliases', [])]]
    if len(matches) != 1:
        raise OrderError('연결된 거래처가 없거나 이름이 중복됩니다. 담당자에게 확인해 주세요.')
    return matches[0]


def build_order(parsed, binding, config, catalog, partners=None):
    user_id = binding.get('userId')
    if not positive_id(user_id):
        raise OrderError('회원 연결 설정을 확인해 주세요.')
    partner = select_partner(parsed, binding, config.get('partners', []) if partners is None else partners)
    if not all(isinstance(partner.get(k), str) and partner[k].strip()
               for k in ('name', 'contactName', 'phone', 'address')):
        raise OrderError('거래처의 담당자·연락처·배송 주소가 필요합니다.')
    field = config.get('price_field', 'wholesalePrice')
    if field not in PRICE_FIELDS:
        raise OrderError('적용 단가 설정을 확인해 주세요.')
    items, codes, total = [], set(), Decimal('0')
    for item in parsed['items']:
        rules = [r for r in config.get('product_map', [])
                 if r.get('input_unit') == item['unit']
                 and re.fullmatch(r['match'], item['name'], re.I)]
        if len(rules) != 1:
            raise OrderError(f"상품·규격·단위 연결을 확인해 주세요: {item['name']} ({item['unit']})")
        rule = rules[0]
        code = rule.get('code')
        matches = [p for p in catalog if code and p.get('code') == code]
        if len(matches) != 1 or code in codes:
            raise OrderError('상품 코드가 없거나 중복되었습니다. 같은 상품은 한 줄로 입력해 주세요.')
        product = matches[0]
        name = product.get('productName')
        if not name or sum(p.get('productName') == name for p in catalog) != 1:
            raise OrderError('주문 API가 상품명으로 처리하므로 중복 상품명은 담당자 확인이 필요합니다.')
        if product.get('openStock') is False:
            raise OrderError('공개 주문 대상이 아닌 상품입니다.')
        # StockInventory.unit is numeric. It does not by itself prove a box/ea conversion.
        annotations = re.findall(r'[（(]\s*(박스|개|병|통|팩|봉|세트)\s*[）)]',
                                 name + ' ' + str(product.get('spec') or ''))
        known_units = set(annotations)
        declared = rule.get('catalog_unit')
        if declared:
            known_units.add(declared)
        if known_units != {item['unit']} or rule.get('quantity_multiplier', 1) != 1:
            raise OrderError('입력 단위와 재고 상품 단위가 다르거나 불명확합니다. 박스·개 환산은 자동으로 하지 않습니다.')
        try:
            price = Decimal(str(product[field]))
            if not price.is_finite() or price < 0:
                raise ValueError()
        except (KeyError, InvalidOperation, ValueError):
            raise OrderError('상품 단가를 확인해 주세요.') from None
        codes.add(code)
        total += price * item['qty']
        items.append({'productName': name, 'quantity': item['qty'], 'price': float(price)})
    shipping = config.get('shipment', {})
    time = shipping.get('estimated_time')
    try:
        window = dt.datetime.fromisoformat(parsed['ship_date'] + 'T' + time)
        if window.utcoffset() is None:
            raise ValueError()
    except (TypeError, ValueError):
        raise OrderError('배송 예정 시간과 시간대 설정이 필요합니다.') from None
    shipment = {'fulfillmentType': 'PARCEL', 'deliveryAddress': partner['address'],
                'estimatedWindow': window.isoformat()}
    if shipping.get('carrier'):
        shipment['carrier'] = shipping['carrier']
    region = {'NAMBU': '남부(기장)', 'JUNGBU': '중부(덕소)', 'SEOBU': '서부(소사)'}.get(binding.get('storeRegion'))
    if not region:
        raise OrderError('회원의 주문 매장 지역 설정이 필요합니다.')
    # OrdersService reads operational fields from slash-delimited notes.
    for value in (partner['name'], partner['contactName'], partner['phone'], partner['address']):
        if any(c in value for c in ('/', '\n', '\r')):
            raise OrderError('거래처 정보에 주문 메모 구분 문자가 있습니다. 담당자 확인이 필요합니다.')
    # CreateOrderDto marks these optional; OrdersService assigns SYN... server-side.
    payload = {'userId': user_id,
               'status': 'PLACED', 'totalAmount': float(total),
               'notes': (f"개별택배 / 택배업체명:{partner['name']} / 택배발송일:{parsed['ship_date']}"
                         f" / 받는분:{partner['contactName']} / 연락처:{partner['phone']}"
                         f" / 받는분주소:{partner['address']} / 주문작업지역:{region} / 지부매장:{region}"),
               'items': items, 'shipment': shipment}
    preview = summary(parsed) + f"\n받는 분: {partner['contactName']}\n배송 주소: {partner['address']}\n예상 상품금액: {total:,.0f}원"
    snapshot = {k: partner[k] for k in ('name', 'contactName', 'phone', 'address')}
    return {'request': payload, 'partnerId': partner['id'], 'partnerSnapshot': snapshot}, preview
