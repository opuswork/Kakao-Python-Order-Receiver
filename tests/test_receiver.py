import asyncio
import base64
import datetime as dt
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from contract import match_products, search_churches
from orders import OrderError, extract, parse_time
from storage import MemoryStore

SECRET = 'test-only-secret-' * 4
CLIENT = TestClient(main.app)
HEADERS = {'X-Kakao-Skill-Key': SECRET}
ORDER = '2099-11-10, 1급진간장 1.8L 10박스, 청운초등학교'

CATALOG = [
    {'code': 'A', 'productName': '* 1급진간장 1.8L(박스)', 'wholesalePrice': 15000, 'category': '일반품'},
    {'code': 'B', 'productName': '* 1급진간장 1.8L(개)', 'wholesalePrice': 2500, 'category': '일반품'},
    {'code': 'C', 'productName': '* 진간장 360mL(박스)', 'wholesalePrice': 12000, 'category': '일반품'},
    {'code': 'D', 'productName': '* 양조진간장 360mL(박스)', 'wholesalePrice': 13000, 'category': '일반품'},
    {'code': 'E', 'productName': '명절 선물세트 1호(박스)', 'wholesalePrice': 30000, 'category': '선물세트'},
]
CHURCHES = [{'id': 3, 'name': '서대문남', 'region': '서울', 'branchCode': '7'},
            {'id': 4, 'name': '서대문북', 'region': '서울', 'branchCode': '8'},
            {'id': 5, 'name': '원주', 'region': '강원', 'branchCode': '5'}]
MEMBER = {'id': 55, 'fullname': '이용식', 'phone': '010-2778-1400', 'memberType': 'GWANJANG',
          'churchId': 3, 'churchName': '서대문남'}
PARTNER = {'id': 7, 'name': '청운초등학교', 'contactName': '함은숙', 'phone': '010-1111-2222',
           'address': '서울 종로구 자하문로 1'}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv('KAKAO_SKILL_SECRET', SECRET)
    monkeypatch.setenv('DRY_RUN', 'true')
    monkeypatch.delenv('RECEIVER_DATABASE_URL', raising=False)
    monkeypatch.setattr(main, 'memory', MemoryStore())


@pytest.fixture
def api(monkeypatch):
    backend = AsyncMock()
    backend.admin_id = 1
    backend.login.return_value = {'Authorization': 'Bearer fake'}
    backend.catalog.return_value = CATALOG
    backend.churches.return_value = CHURCHES
    backend.members.side_effect = lambda http, headers, name: [MEMBER] if name in MEMBER['fullname'] else []
    backend.partners.side_effect = lambda http, headers, q: [PARTNER] if q in PARTNER['name'] else []
    backend.create.return_value = httpx.Response(201, json={'id': 9, 'userId': 55, 'orderNumber': 'SYN0123456789'})
    monkeypatch.setattr(main, 'Backend', lambda: backend)
    monkeypatch.setattr(main, 'client', AsyncMock)  # No TLS setup per message in tests.
    return backend


@pytest.fixture
def live(monkeypatch, api):
    monkeypatch.setenv('DRY_RUN', 'false')
    monkeypatch.setenv('RECEIVER_DATABASE_URL', 'unused-mocked-database')
    state = MemoryStore()
    monkeypatch.setattr(main, 'store', state)
    return state, api


def say(text, owner='u1'):
    r = CLIENT.post('/kakao/order', headers=HEADERS, json={'userRequest': {'utterance': text, 'user': {'id': owner}}})
    assert r.status_code == 200, r.text
    template = r.json()['template']
    return template['outputs'][0]['simpleText']['text'], [q['messageText'] for q in template.get('quickReplies', [])]


def complete_new_orderer(owner='u1'):
    say(ORDER, owner)
    say('중부', owner)
    say('홍길동 010-1234-5678', owner)
    text, choices = say('서대문', owner)
    assert choices == ['서대문남', '서대문북']
    say('서대문남', owner)
    say('맞아요', owner)
    return say('택배', owner)


# ---------------------------------------------------------------- receiver basics

def test_auth_and_malformed():
    body = {'userRequest': {'utterance': '연결확인', 'user': {'id': 'u1'}}}
    assert CLIENT.post('/kakao/order', json=body).status_code == 401
    assert CLIENT.post('/kakao/order', headers=HEADERS, json={}).status_code == 400
    assert CLIENT.post('/kakao/order', headers=HEADERS, content='a'*33000).status_code == 413


def test_missing_secret(monkeypatch):
    monkeypatch.delenv('KAKAO_SKILL_SECRET')
    body = {'userRequest': {'utterance': '연결확인', 'user': {'id': 'u1'}}}
    assert CLIENT.post('/kakao/order', headers=HEADERS, json=body).status_code == 503


def test_live_mode_requires_database(monkeypatch, api):
    monkeypatch.setenv('DRY_RUN', 'false')
    text, _ = say(ORDER)
    assert '오류' in text and api.create.await_count == 0


def test_greeting_does_not_start_an_order(api):
    text, _ = say('안녕하세요')
    assert '주문 내용을 보내 주세요' in text and api.catalog.await_count == 0


# ---------------------------------------------------------------- extraction

def test_extract_labelled_chat_order():
    found = extract('27일납품\n진간장 360ml 2박스\n청운초등학교\n\n주문자 성명: 이용식,\n전화번호: 01027781400\n'
                    '배달시간: 05:00\n주문매장: 중부\n상세주소: 서소문로 55-6 으로 검색, 상세주소:1층',
                    dt.date(2026, 10, 14))
    assert found['date'] == '2026-10-27' and found['store'] == 'JUNGBU'
    assert found['items'] == [{'name': '진간장 360ml', 'qty': 2, 'unit': '박스'}]
    assert (found['orderer_name'], found['orderer_phone']) == ('이용식', '010-2778-1400')
    assert found['time'] == {'ampm': '오전', 'time': '05:00'}
    assert found['unknown'] == ['청운초등학교'] and found['address'] == '서소문로 55-6 으로 검색 1층'


@pytest.mark.parametrize('text', [
    '11월 10일, 1급진간장 1.8리터 10개, 신선유통 주문합니다.(테스트)',
    '1급진간장 1.8리터 10개 / 신선유통 / 11/10 납품',
    '신선유통, 11월 10일까지, 1급진간장 1.8리터 10개 주문 부탁드립니다!',
])
def test_any_part_order_and_courtesy_endings(text):
    found = extract(text, dt.date(2026, 10, 27))
    assert found['date'] == '2026-11-10' and found['unknown'] == ['신선유통']
    assert found['items'] == [{'name': '1급진간장 1.8리터', 'qty': 10, 'unit': '개'}]


def test_day_only_date_rolls_to_next_month():
    assert extract('3일 납품, 진간장 1박스', dt.date(2026, 10, 28))['date'] == '2026-11-03'
    assert extract('3일 납품, 진간장 1박스', dt.date(2026, 12, 28))['date'] == '2027-01-03'


@pytest.mark.parametrize('text', [
    '신선유통, 2099-02-30, 진간장 3박스',
    '신선유통, 2099-10-21, 진간장 0박스',
    '신선유통, 2099-10-21, 진간장 3박스, 진간장 3박스',
    '2099-11-10, 2099-11-11, 진간장 3박스',
    '2099-11-10, 진간장 3박스, 남부, 서부',
    '신선유통, 9월 21일, 진간장 3박스',
])
def test_invalid_or_ambiguous_inputs_rejected(text):
    with pytest.raises(OrderError):
        extract(text, dt.date(2026, 10, 14))


@pytest.mark.parametrize('text, expected', [
    ('오전 10시', ('오전', '10:00')), ('오후 2시 반', ('오후', '02:30')), ('14:30', ('오후', '02:30')),
    ('12시', ('오후', '12:00')), ('새벽 5시', ('오전', '05:00')),
])
def test_delivery_time(text, expected):
    t = parse_time(text)
    assert (t['ampm'], t['time']) == expected


def test_product_matching_rules():
    assert [p['code'] for p in match_products(CATALOG, {'name': '1급진간장 1.8리터', 'unit': '박스'})] == ['A']
    assert [p['code'] for p in match_products(CATALOG, {'name': '1급진간장 1.8L', 'unit': '개'})] == ['B']
    assert [p['code'] for p in match_products(CATALOG, {'name': '진간장 360ml', 'unit': '박스'})] == ['C']
    assert {p['code'] for p in match_products(CATALOG, {'name': '간장 360ml', 'unit': '박스'})} == {'C', 'D'}
    assert match_products(CATALOG, {'name': '1급진간장 1.8L', 'unit': '병'}) == []
    assert match_products(CATALOG, {'name': '명절 선물세트 1호', 'unit': '박스'}) == []


def test_church_search_matches_order_form():
    assert [c['name'] for c in search_churches(CHURCHES, '5')] == ['원주']
    assert [c['name'] for c in search_churches(CHURCHES, '서울')] == ['서대문남', '서대문북']
    assert [c['name'] for c in search_churches(CHURCHES, '서대문남')] == ['서대문남']


# ---------------------------------------------------------------- dialog

def test_asks_only_missing_fields_then_previews(api):
    text, choices = say(ORDER)
    assert '매장' in text and choices == ['남부', '중부', '서부']
    text, _ = say('중부')
    assert '주문자 성함과 전화번호' in text
    text, _ = say('홍길동 010-1234-5678')
    assert '중앙을 검색' in text
    say('서대문')
    text, choices = say('서대문남')
    assert '함은숙' in text and choices == ['맞아요', '직접 입력']
    text, choices = say('맞아요')
    assert choices == ['택배', '배달']
    text, choices = say('택배')
    assert '[테스트 모드]' in text and '신규 회원' in text and '150,000원' in text
    assert choices[0].startswith('확정 ')
    text, _ = say(choices[0])
    assert '주문앱에는 보내지 않았습니다' in text and api.create.await_count == 0


def test_wrong_answer_reasks_same_question(api):
    say(ORDER)
    text, choices = say('동부')
    assert '남부, 중부, 서부' in text and choices == ['남부', '중부', '서부']


def test_profile_is_offered_and_proxy_order_asks_again(api):
    complete_new_orderer()
    text, choices = say('2099-11-10, 1급진간장 1.8L 12박스, 청운초등학교, 남부')
    assert '홍길동 님(010-1234-5678)인가요' in text and choices == ['네', '다른 분 주문']
    text, _ = say('다른 분 주문')
    assert '주문자 성함과 전화번호' in text
    text, _ = say('이용식 010-2778-1400')
    assert '받는분: 함은숙' in text  # existing member: church comes from the member


def test_ambiguous_product_choice(api):
    text, choices = say('2099-11-10, 간장 360ml 2박스, 청운초등학교')
    assert choices == ['1', '2'] and '진간장 360mL(박스)' in text
    text, _ = say('2')
    assert '매장' in text


def test_unknown_product_reasks(api):
    text, _ = say('2099-11-10, 고추장 2박스, 청운초등학교')
    assert '찾지 못했습니다' in text
    text, _ = say('진간장 360ml 2박스')
    assert '매장' in text


def test_unknown_partner_asks_for_recipient(api):
    text, _ = say('2099-11-10, 진간장 360ml 2박스, 신선유통, 남부, 이용식 010-2778-1400')
    assert '거래처관리에' in text and '성함, 연락처, 주소' in text
    say('김철수 010-3333-4444 부산 해운대구 1')
    text, choices = say('배달')
    assert '배달 시간' in text
    text, choices = say('오전 10시')
    assert '김철수' in text and choices[0].startswith('확정 ')


def test_cancel_words_clear_session(api):
    say(ORDER)
    text, _ = say('취소')
    assert '취소했습니다' in text
    text, _ = say('중부')
    assert '주문 내용을 보내 주세요' in text


def test_new_full_order_replaces_session(api):
    say(ORDER)
    text, _ = say('2099-11-12, 진간장 360ml 2박스, 청운초등학교')
    assert '매장' in text


# ---------------------------------------------------------------- submission

def decode_lines(notes):
    blob = notes.split('배송상세:')[1]
    return json.loads(base64.urlsafe_b64decode(blob + '=' * (-len(blob) % 4)))['lines']


def test_existing_member_delivery_payload(live):
    state, api = live
    say('2099-11-10, 진간장 360ml 2박스, 청운초등학교, 남부, 배달, 오전 10시, 주문자: 이용식 010-2778-1400')
    _, choices = say('맞아요')
    text, _ = say(choices[0])
    assert 'SYN0123456789' in text
    payload = api.create.call_args.args[2]
    assert payload['userId'] == 55 and 'ordererProfile' not in payload
    assert payload['items'] == [{'productName': '* 진간장 360mL(박스)', 'quantity': 2, 'price': 12000.0}]
    assert payload['totalAmount'] == 24000
    notes = payload['notes']
    for segment in ('주문자:이용식관', '연락처:010-2778-1400', '중앙:서대문남', '배달업체명:청운초등학교',
                    '배달일:2099-11-10 오전 10:00', '받는분:함은숙 / 010-1111-2222 / 서울 종로구 자하문로 1',
                    '주문작업지역:남부(기장)', '지부매장:남부(기장)', '인사장종류:없음',
                    '[배달] * 진간장 360mL(박스) 2개'):
        assert segment in notes
    assert payload['shipment'] == {'fulfillmentType': 'PARCEL', 'carrier': '청운초등학교',
                                   'deliveryAddress': '서울 종로구 자하문로 1',
                                   'estimatedWindow': '2099-11-10T10:00:00.000Z'}
    line = decode_lines(notes)[0]
    assert line['product'] == '* 진간장 360mL(박스)' and line['ship']['deliveryTime'] == '10:00'
    assert payload['extraNote'].startswith('카카오접수:')


def test_new_orderer_parcel_payload(live):
    state, api = live
    api.create.return_value = httpx.Response(201, json={'id': 10, 'userId': 99, 'orderNumber': 'SYN0000000010'})
    text, choices = complete_new_orderer()
    text, _ = say(choices[0])
    assert 'SYN0000000010' in text
    payload = api.create.call_args.args[2]
    assert payload['userId'] == 1
    assert payload['ordererProfile'] == {'fullname': '홍길동', 'phone': '010-1234-5678', 'churchId': 3}
    assert '택배발송일:2099-11-10' in payload['notes'] and '주문작업지역:중부(덕소)' in payload['notes']
    assert '보내는사람:홍길동 / 010-1234-5678 / 서울 종로구 자하문로 1' in payload['notes']
    assert payload['shipment']['estimatedWindow'] == '2099-11-10T09:00:00.000Z'


def test_confirm_replay_and_duplicate_order(live):
    state, api = live
    text, choices = complete_new_orderer()
    say(choices[0])
    say(choices[0])
    assert api.create.await_count == 1


def test_other_user_cannot_confirm(live):
    state, api = live
    _, choices = complete_new_orderer()
    text, _ = say(choices[0], owner='u2')
    assert '찾지 못했습니다' in text and api.create.await_count == 0


def test_timeout_never_reposts(live):
    state, api = live
    api.create.side_effect = httpx.ReadTimeout('simulated')
    _, choices = complete_new_orderer()
    text, _ = say(choices[0])
    assert '확인이 필요' in text
    say(choices[0])
    identifier = choices[0].split()[1]
    assert api.create.await_count == 1 and state.rows[identifier]['status'] == 'unknown'


def test_expired_draft(live):
    state, api = live
    _, choices = complete_new_orderer()
    state.rows[choices[0].split()[1]]['fresh'] = False
    text, _ = say(choices[0])
    assert '만료' in text and api.create.await_count == 0


def test_cancelled_draft(live):
    state, api = live
    _, choices = complete_new_orderer()
    say(choices[1])
    say(choices[0])
    assert api.create.await_count == 0


def test_concurrent_confirm_claims_once(live):
    state, api = live
    _, choices = complete_new_orderer()
    body = main.SkillRequest.model_validate({'userRequest': {'utterance': choices[0], 'user': {'id': 'u1'}}})

    async def run():
        return await asyncio.gather(main.process(body), main.process(body))
    asyncio.run(run())
    assert api.create.await_count == 1


@pytest.mark.parametrize('status, result', [
    (202, {'id': 9, 'userId': 99, 'orderNumber': 'SYN1'}),
    (201, {'id': 9, 'userId': 99}),
    (201, {'id': 0, 'userId': 99, 'orderNumber': 'SYN1'}),
    (201, {'id': 9, 'orderNumber': 'SYN1'}),
])
def test_unconfirmed_api_response_never_reports_success(live, status, result):
    state, api = live
    api.create.return_value = httpx.Response(status, json=result)
    _, choices = complete_new_orderer()
    text, _ = say(choices[0])
    assert '접수되었습니다' not in text


def test_existing_member_response_must_match_owner(live):
    state, api = live
    api.create.return_value = httpx.Response(201, json={'id': 9, 'userId': 77, 'orderNumber': 'SYN1'})
    say('2099-11-10, 진간장 360ml 2박스, 청운초등학교, 남부, 택배, 주문자: 이용식 010-2778-1400')
    _, choices = say('맞아요')
    text, _ = say(choices[0])
    assert '확인이 필요' in text


def test_nonadmin_token_rejected_before_any_order(monkeypatch):
    from backend import Backend
    claims = base64.urlsafe_b64encode(json.dumps({'sub': 5, 'role': 'member'}).encode()).decode().rstrip('=')
    monkeypatch.setenv('SANC_API_TOKEN', f'h.{claims}.s')
    with pytest.raises(ValueError):
        asyncio.run(Backend().login(None))


def test_admin_token_gives_admin_id(monkeypatch):
    from backend import Backend
    claims = base64.urlsafe_b64encode(json.dumps({'sub': 284, 'role': 'admin'}).encode()).decode().rstrip('=')
    monkeypatch.setenv('SANC_API_TOKEN', f'h.{claims}.s')
    api = Backend()
    asyncio.run(api.login(None))
    assert api.admin_id == 284 and api.base == 'https://be-aosm.onrender.com'
