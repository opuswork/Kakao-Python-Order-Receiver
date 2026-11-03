"""Chat version of the 제품주문서: fills the form from the first message, then asks what is missing.

State is a plain JSON dict kept per Kakao user (storage.Store sessions). Lookups go through ctx:
  ctx.catalog(), ctx.members(name), ctx.churches(), ctx.partners(q), ctx.profile, ctx.today
"""
import re

from contract import match_products, price_of, search_churches
from orders import (OrderError, clean, kind_of, parse_date, parse_item, parse_person, parse_time,
                    split_parts, store_of, valid_name)

CANCEL_WORDS = {'취소', '처음부터', '주문 취소', '주문취소', '그만', '다시'}
EXAMPLE = '예: 11월 10일, 1급진간장 1.8L 10박스, 청운초등학교'
MAX_CHOICES = 10


class Reply:
    def __init__(self, text, choices=None):
        self.text = text
        self.choices = choices or []


def new_state(found):
    s = {'raw_items': found['items'], 'items': [], 'awaiting': None, 'options': []}
    for key in ('date', 'store', 'kind', 'time', 'company', 'church'):
        if found.get(key):
            s[key] = found[key]
    unknown = list(found['unknown'])
    if found.get('orderer_name') or found.get('orderer_phone'):
        s['orderer'] = {'name': found.get('orderer_name', ''), 'phone': found.get('orderer_phone', '')}
    recipient = {'name': found.get('recipient_name', ''), 'phone': found.get('recipient_phone', ''),
                 'address': found.get('address', '')}
    # "15일납품 / 국간장 4.2L 1통 / 총운초등학교 / 함은숙": 업체 then 받는분 성함.
    if 'company' not in s and unknown:
        s['company'] = unknown.pop(0)
    if unknown and not recipient['name'] and valid_name(unknown[0]) and len(unknown[0]) <= 5:
        recipient['name'] = unknown.pop(0)
    if unknown:
        raise OrderError('이해하지 못한 내용이 있습니다: ' + ', '.join(unknown) +
                         '\n항목은 쉼표나 줄바꿈으로 나눠 주세요. ' + EXAMPLE)
    if any(recipient.values()):
        s['recipient'] = recipient
    return s


def is_order_like(found):
    return bool(found['items'] or found.get('date'))


def buttons(labels, messages=None):
    return [(label[:14], message) for label, message in zip(labels, messages or labels)]


def numbered(options, label):
    lines = [f'{n}. {label(o)}' for n, o in enumerate(options, 1)]
    return '\n'.join(lines), buttons([f'{n}. {label(o)}' for n, o in enumerate(options, 1)],
                                     [str(n) for n in range(1, len(options) + 1)])


def pick(s, text):
    """A numbered or exact-label answer to the last list of options."""
    text = text.strip()
    if text.isdigit() and 1 <= int(text) <= len(s['options']):
        return s['options'][int(text) - 1]
    for option in s['options']:
        if text in (option.get('productName'), option.get('name')):
            return option
    return None


async def answer(s, text, ctx):
    """Apply a reply to the question in s['awaiting']. Raises OrderError to re-ask."""
    step = s.get('awaiting')
    text = text.strip()
    if step == 'items':
        items = [i for i in map(parse_item, split_parts(text)) if i]
        if not items:
            raise OrderError('상품명 뒤에 수량과 단위를 써 주세요. 예: 1급진간장 1.8L 10박스')
        s['raw_items'] += items
    elif step == 'item_choice':
        product = pick(s, text)
        if not product:
            raise OrderError('목록의 번호를 눌러 주세요.')
        raw = s['raw_items'].pop(0)
        add_item(s, product, raw['qty'])
    elif step == 'date':
        date = parse_date(text, ctx.today)
        if not date:
            raise OrderError('날짜는 11월 10일, 11/10 또는 2026-11-10 형식으로 입력해 주세요.')
        s['date'] = date
    elif step == 'store':
        s['store'] = store_of(text) or fail('남부, 중부, 서부 중 하나를 눌러 주세요.')
    elif step == 'orderer_confirm':
        if text == '네':
            s['orderer'] = {**ctx.profile}
        elif text == '다른 분 주문':
            s['orderer_other'] = True
        else:
            set_orderer(s, text)
            s['orderer_other'] = True
    elif step == 'orderer':
        set_orderer(s, text)
    elif step in ('church', 'church_choice'):
        church = pick(s, text) if step == 'church_choice' else None
        if church:
            set_church(s, church)
        else:
            s['church_query'] = text
    elif step == 'company':
        s['company'] = clean(text) or fail('받는 곳(업체) 이름을 입력해 주세요.')
        for key in ('recipient', 'recipient_checked', 'recipient_manual'):
            s.pop(key, None)
    elif step == 'receiver_choice':
        partner = pick(s, text)
        if partner:
            s['recipient'] = recipient_of(partner)
            s['recipient_checked'] = True
        elif text == '직접 입력':
            s['recipient_manual'] = True
        else:
            raise OrderError('목록의 번호를 누르거나 [직접 입력]을 눌러 주세요.')
    elif step == 'receiver_confirm':
        if text == '맞아요':
            s['recipient_checked'] = True
        elif text == '직접 입력':
            s['recipient_manual'] = True
            s.pop('recipient', None)
        else:
            set_recipient(s, text)
    elif step == 'receiver_manual':
        set_recipient(s, text)
    elif step == 'kind':
        s['kind'] = kind_of(text) or fail('택배 또는 배달을 눌러 주세요.')
    elif step == 'time':
        s['time'] = parse_time(text) or fail('배달 시간을 알려 주세요. 예: 오전 10시, 14:30')
    s['awaiting'] = None


def fail(message):
    raise OrderError(message)


def set_orderer(s, text):
    person = parse_person(text)
    if not person or not valid_name(person['name']) or len(person['name']) < 2 or person['rest']:
        raise OrderError('주문자 성함과 전화번호를 함께 적어 주세요. 예: 홍길동 010-1234-5678')
    s['orderer'] = {'name': person['name'], 'phone': person['phone']}


def set_recipient(s, text):
    person = parse_person(text)
    if not person or not person['name'] or not person['rest']:
        raise OrderError('받는분 성함, 연락처, 주소를 한 줄로 적어 주세요.\n예: 함은숙 010-1234-5678 서울 종로구 서소문로 55-6 1층')
    s['recipient'] = {'name': person['name'], 'phone': person['phone'], 'address': person['rest']}
    s['recipient_checked'] = True


def set_church(s, church):
    s['orderer'].update(churchId=church['id'], churchName=church['name'])
    s.pop('church_query', None)


def recipient_of(partner):
    return {'name': clean(partner.get('contactName')), 'phone': clean(partner.get('phone')),
            'address': clean(partner.get('address'))}


def add_item(s, product, qty):
    if any(i['productName'] == product['productName'].strip() for i in s['items']):
        raise OrderError('같은 상품이 두 번 들어왔습니다. 수량을 합쳐 다시 입력해 주세요.')
    s['items'].append({'productName': product['productName'].strip(), 'quantity': qty,
                       'price': float(price_of(product))})


def ask(s, step, text, choices=None, options=None):
    s['awaiting'] = step
    s['options'] = options or []
    return Reply(text, choices)


async def next_question(s, ctx):
    """Ask for the first missing field, or return None when the form is complete."""
    if s['raw_items']:
        catalog = await ctx.catalog()
        while s['raw_items']:
            raw = s['raw_items'][0]
            found = match_products(catalog, raw)
            label = f"{raw['name']} {raw['qty']}{raw['unit']}"
            if not found:
                s['raw_items'].pop(0)
                return ask(s, 'items', f"'{label}' 상품을 찾지 못했습니다. 상품명과 단위를 확인해 다시 입력해 주세요.\n"
                                       '(선물세트는 인사장이 필요해 앱에서 주문해 주세요.)')
            if len(found) > 1:
                options = found[:MAX_CHOICES]
                listing, choices = numbered(options, lambda p: p['productName'].strip())
                return ask(s, 'item_choice', f"'{label}'에 해당하는 상품을 골라 주세요.\n{listing}", choices, options)
            s['raw_items'].pop(0)
            add_item(s, found[0], raw['qty'])
    if not s['items']:
        return ask(s, 'items', '주문할 상품과 수량을 알려 주세요. 여러 개면 줄바꿈해 주세요.\n예: 1급진간장 1.8L 10박스')
    if not s.get('date'):
        return ask(s, 'date', '납품(발송) 날짜를 알려 주세요. 예: 11월 10일')
    if not s.get('store'):
        return ask(s, 'store', '어느 매장 주문인가요?', buttons(['남부', '중부', '서부']))
    o = s.get('orderer')
    if not o or not (o.get('name') and o.get('phone')):
        if ctx.profile and not s.get('orderer_other') and not o:
            p = ctx.profile
            return ask(s, 'orderer_confirm', f"주문자는 {p['name']} 님({p['phone']})인가요?",
                       buttons(['네', '다른 분 주문']))
        return ask(s, 'orderer', '주문자 성함과 전화번호를 알려 주세요. 예: 홍길동 010-1234-5678')
    if 'checked' not in o:
        # Same lookup as the admin form's autocomplete; phone decides who it is.
        digits = re.sub(r'\D', '', o['phone'])
        members = [m for m in await ctx.members(o['name'])
                   if re.sub(r'\D', '', m.get('phone') or '') == digits]
        o['checked'] = True
        if members:
            m = members[0]
            o.update(userId=m['id'], memberType=m.get('memberType'), churchId=m.get('churchId'),
                     churchName=m.get('churchName'))
        if not ctx.profile and not s.get('orderer_other'):
            s['save_profile'] = True
    if not o.get('churchName') or not o.get('churchId'):
        query = s.pop('church_query', None) or s.pop('church', None)
        if query:
            found = search_churches(await ctx.churches(), query)
            if len(found) == 1:
                set_church(s, found[0])
            elif found:
                options = found[:MAX_CHOICES]
                more = f'\n(검색 결과 {len(found)}곳 중 {len(options)}곳만 보여 드립니다. 더 자세히 입력해 주세요.)' \
                    if len(found) > len(options) else ''
                return ask(s, 'church_choice', '중앙을 골라 주세요.' + more,
                           buttons([c['name'] for c in options]), options)
            else:
                return ask(s, 'church', f"'{query}'로 중앙을 찾지 못했습니다. 다시 검색해 주세요. (예: 5, 서울, 원주)")
        else:
            return ask(s, 'church', f"{o['name']} 님의 중앙을 검색해 주세요. (예: 5, 서울, 원주)")
    if not s.get('company'):
        return ask(s, 'company', '받는 곳(업체·거래처) 이름을 알려 주세요. 예: 청운초등학교')
    r = s.get('recipient') or {}
    if not s.get('recipient_checked'):
        if r.get('name') and r.get('phone') and r.get('address'):
            s['recipient_checked'] = True
        elif s.get('recipient_manual'):
            return ask(s, 'receiver_manual', '받는분 성함, 연락처, 주소를 한 줄로 적어 주세요.\n'
                                             '예: 함은숙 010-1234-5678 서울 종로구 서소문로 55-6 1층')
        else:
            partners = [p for p in await ctx.partners(s['company']) if (p.get('name') or '').strip() == s['company']]
            if len(partners) == 1:
                s['recipient'] = recipient_of(partners[0])
                r = s['recipient']
                return ask(s, 'receiver_confirm', f"거래처에서 '{s['company']}'를 찾았습니다.\n"
                                                  f"받는분: {r['name']} {r['phone']}\n주소: {r['address']}",
                           buttons(['맞아요', '직접 입력']))
            if partners:
                options = partners[:MAX_CHOICES]
                listing, choices = numbered(options, lambda p: f"{p.get('contactName')} {p.get('address')}")
                return ask(s, 'receiver_choice', f"'{s['company']}' 거래처가 여러 곳입니다.\n{listing}",
                           choices + buttons(['직접 입력']), options)
            s['recipient_manual'] = True
            return ask(s, 'receiver_manual', f"거래처관리에 '{s['company']}'가 없습니다.\n"
                                             '받는분 성함, 연락처, 주소를 한 줄로 적어 주세요.\n'
                                             '예: 함은숙 010-1234-5678 서울 종로구 서소문로 55-6 1층')
    if not s.get('kind'):
        return ask(s, 'kind', '배송 방법을 골라 주세요.', buttons(['택배', '배달']))
    if s['kind'] == 'delivery' and not s.get('time'):
        return ask(s, 'time', '배달 시간을 알려 주세요. 예: 오전 10시, 14:30')
    s['awaiting'] = None
    return None
