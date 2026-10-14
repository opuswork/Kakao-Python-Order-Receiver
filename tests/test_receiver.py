import asyncio
import copy
import datetime as dt
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from orders import OrderError, parse_order
from contract import build_order

SECRET = 'test-only-secret-' * 4
CLIENT = TestClient(main.app)
HEADERS = {'X-Kakao-Skill-Key': SECRET}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv('KAKAO_SKILL_SECRET', SECRET)
    monkeypatch.setenv('DRY_RUN', 'true')
    main.config.cache_clear()


def data(text='신선유통, 2099-10-21, 1급진간장 1.8리터 300박스', owner='u1'):
    return {'userRequest': {'utterance': text, 'user': {'id': owner}}}


def message(response):
    return response.json()['template']['outputs'][0]['simpleText']['text']


def test_dry_run_no_backend_or_db(monkeypatch):
    def forbidden(*args):
        raise AssertionError('dry run must not contact backend')
    monkeypatch.setattr(main, 'Backend', forbidden)
    r = CLIENT.post('/kakao/order', headers=HEADERS, json=data())
    assert r.status_code == 200 and '300박스' in message(r) and '저장하지 않았습니다' in message(r)


def test_auth_and_malformed():
    assert CLIENT.post('/kakao/order', json=data()).status_code == 401
    assert CLIENT.post('/kakao/order', headers=HEADERS, json={}).status_code == 400
    assert CLIENT.post('/kakao/order', headers=HEADERS, content='a'*33000).status_code == 413


def test_missing_secret(monkeypatch):
    monkeypatch.delenv('KAKAO_SKILL_SECRET')
    assert CLIENT.post('/kakao/order', headers=HEADERS, json=data()).status_code == 503


@pytest.mark.parametrize('text', [
    '신선유통, 2099-02-30, 진간장 3박스',
    '신선유통, 2099-10-21, 진간장 0박스',
    '신선유통, 2099-10-21, 진간장 세박스',
    '신선유통, 2099-10-21, 진간장 3박스 취소',
    '신선유통, 2099-10-21, 진간장 3박스, 진간장 3박스',
    '신선유통, 내일, 진간장 3박스',
])
def test_ambiguous_inputs_not_submitted(text):
    with pytest.raises(OrderError):
        parse_order(text)


def test_year_and_multiple_items():
    p = parse_order('신선유통, 10월 21일, 진간장 300박스\n국간장 3통', dt.date(2026, 10, 14))
    assert p['ship_date'] == '2026-10-21' and len(p['items']) == 2
    with pytest.raises(OrderError):
        parse_order('신선유통, 9월 21일, 진간장 3박스', dt.date(2026, 10, 14))


def configuration():
    return {'partner_source': 'config', 'bindings': {'u1': {'userId': 123, 'allowed_partner_ids': [7], 'storeRegion': 'NAMBU'}},
            'partners': [{'id': 7, 'userId': 123, 'name': '신선유통', 'contactName': '테스트',
                          'phone': '01000000000', 'address': '테스트 주소'}],
            'shipment': {'estimated_time': '09:00:00+09:00'},
            'product_map': [{'match': r'1급진간장\s*1[.]8리터', 'input_unit': '박스',
                             'code': 'TEST-BOX', 'catalog_unit': '박스'}]}


def catalog():
    return [{'code': 'TEST-BOX', 'productName': 'TEST 1.8L(박스)',
             'spec': '1.8L(박스)', 'wholesalePrice': 100, 'openStock': True}]


def test_unit_and_customer_authorization():
    cfg = configuration()
    p = parse_order(data()['userRequest']['utterance'])
    b = cfg['bindings']['u1']
    assert build_order(p, b, cfg, catalog())[0]['request']['items'][0]['quantity'] == 300
    p['items'][0]['unit'] = '병'
    with pytest.raises(OrderError):
        build_order(p, b, cfg, catalog())
    p['items'][0]['unit'] = '박스'
    with pytest.raises(OrderError):
        build_order(p, {**b, 'allowed_partner_ids': []}, cfg, catalog())


class MemoryStore:
    """Test-only substitute. Production always uses PostgreSQL."""
    def __init__(self):
        self.rows = {}

    async def draft(self, owner, fingerprint, payload, preview, identifier):
        old = next((r for r in self.rows.values() if r['fingerprint'] == fingerprint), None)
        if old:
            return copy.deepcopy(old)
        row = dict(id=identifier, owner=owner, fingerprint=fingerprint, payload=payload, preview=preview,
                   status='draft', fresh=True)
        self.rows[identifier] = row
        return copy.deepcopy(row)

    async def get(self, identifier, owner):
        row = self.rows.get(identifier)
        return copy.deepcopy(row) if row and row['owner'] == owner else None

    async def claim(self, identifier, owner):
        row = self.rows.get(identifier)
        if row and row['owner'] == owner and row['status'] == 'draft' and row['fresh']:
            row['status'] = 'posting'
            return copy.deepcopy(row)

    async def finish(self, identifier, owner, status, order_no=None):
        self.rows[identifier].update(status=status, order_no=order_no)

    async def cancel(self, identifier, owner):
        row = self.rows.get(identifier)
        if row and row['owner'] == owner and row['status'] == 'draft':
            row['status'] = 'cancelled'


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv('DRY_RUN', 'false')
    monkeypatch.setenv('RECEIVER_DATABASE_URL', 'unused-mocked-database')
    monkeypatch.setenv('ORDER_CONFIG_JSON', json.dumps(configuration()))
    state = MemoryStore()
    monkeypatch.setattr(main, 'store', state)
    api = AsyncMock()
    api.login.return_value = {'Authorization': 'Bearer fake'}
    api.catalog.return_value = catalog()
    api.create.return_value = httpx.Response(201, json={'id': 9, 'userId': 123, 'orderNumber': 'SYN0123456789'})
    monkeypatch.setattr(main, 'Backend', lambda: api)
    return state, api


def draft_id():
    response = CLIENT.post('/kakao/order', headers=HEADERS, json=data())
    assert response.status_code == 200, response.text
    return response.json()['template']['quickReplies'][0]['messageText'].split()[1]


def test_confirm_then_replay_and_payload(live):
    state, api = live
    identifier = draft_id()
    assert api.create.await_count == 0
    response = CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 ' + identifier))
    assert 'SYN0123456789' in message(response)
    CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 ' + identifier))
    CLIENT.post('/kakao/order', headers=HEADERS, json=data())
    assert api.create.await_count == 1
    payload = api.create.call_args.args[2]
    assert payload['userId'] == 123 and payload['items'][0]['quantity'] == 300
    assert payload['totalAmount'] == 30000


def test_other_user_cannot_confirm(live, monkeypatch):
    state, api = live
    identifier = draft_id()
    cfg = configuration()
    cfg['bindings']['u2'] = {**cfg['bindings']['u1'], 'userId': 456}
    monkeypatch.setenv('ORDER_CONFIG_JSON', json.dumps(cfg))
    main.config.cache_clear()
    r = CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier, 'u2'))
    assert '찾지 못했습니다' in message(r) and api.create.await_count == 0


def test_timeout_never_reposts(live):
    state, api = live
    identifier = draft_id()
    api.create.side_effect = httpx.ReadTimeout('simulated')
    r = CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    assert '확인이 필요' in message(r)
    CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    assert api.create.await_count == 1 and state.rows[identifier]['status'] == 'unknown'


def test_expired_draft(live):
    state, api = live
    identifier = draft_id()
    state.rows[identifier]['fresh'] = False
    r = CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    assert '만료' in message(r) and api.create.await_count == 0


def test_cancelled_draft(live):
    state, api = live
    identifier = draft_id()
    CLIENT.post('/kakao/order', headers=HEADERS, json=data('취소 '+identifier))
    CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    assert api.create.await_count == 0


def test_concurrent_confirm_claims_once(live):
    state, api = live
    identifier = draft_id()
    async def run():
        body = main.SkillRequest.model_validate(data('확정 '+identifier))
        return await asyncio.gather(main.process(body), main.process(body))
    asyncio.run(run())
    assert api.create.await_count == 1


def test_delivery_contract(live):
    state, api = live
    identifier = draft_id()
    CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    payload = api.create.call_args.args[2]
    assert payload['shipment']['deliveryAddress'] == '테스트 주소'
    assert payload['shipment']['estimatedWindow'] == '2099-10-21T09:00:00+09:00'
    assert 'carrier' not in payload['shipment']
    assert 'partnerId' not in payload and 'orderGroupKey' not in payload
    assert 'orderNumber' not in payload
    assert '주문작업지역:남부(기장)' in payload['notes']
    assert '택배발송일:2099-10-21' in payload['notes']
    assert payload['extraNote'] == '카카오접수:' + identifier


def test_shared_partner_creator_is_not_order_owner():
    cfg = configuration()
    cfg['partners'][0]['userId'] = 456
    payload, _ = build_order(parse_order(data()['userRequest']['utterance']), cfg['bindings']['u1'], cfg, catalog())
    assert payload['request']['userId'] == 123


def test_box_cannot_map_to_each_even_if_config_claims_box():
    cfg = configuration()
    products = catalog()
    products[0].update(productName='1급진간장 1.8L (개)', spec='1.8L(개)', unit=1)
    with pytest.raises(OrderError):
        build_order(parse_order(data()['userRequest']['utterance']), cfg['bindings']['u1'], cfg, products)


def test_catalog_name_ambiguity():
    cfg = configuration()
    products = catalog() + [{**catalog()[0], 'code': 'OTHER'}]
    with pytest.raises(OrderError):
        build_order(parse_order(data()['userRequest']['utterance']), cfg['bindings']['u1'], cfg, products)


def test_example_total_is_calculated():
    cfg = configuration()
    cfg['product_map'] = [{'match': '명진1호', 'input_unit': '박스', 'code': 'A', 'catalog_unit': '박스'},
                          {'match': 'S5호', 'input_unit': '박스', 'code': 'B', 'catalog_unit': '박스'}]
    products = [{'code': 'A', 'productName': '명진1호', 'wholesalePrice': 15000},
                {'code': 'B', 'productName': 'S5호', 'wholesalePrice': 12000}]
    parsed = parse_order('신선유통, 2099-10-21, 명진1호 300박스, S5호 200박스')
    payload, _ = build_order(parsed, cfg['bindings']['u1'], cfg, products)
    assert payload['request']['totalAmount'] == 6900000


@pytest.mark.parametrize('status, result', [(202, {'orderNumber': 'pending'}), (200, {'message': 'ok'})])
def test_unconfirmed_api_response_never_reports_success(live, status, result):
    state, api = live
    identifier = draft_id()
    api.create.return_value = httpx.Response(status, json=result)
    r = CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    assert '확인이 필요' in message(r)
    CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    assert api.create.await_count == 1


def test_backend_uses_supplied_origin_and_token(monkeypatch):
    from backend import Backend
    monkeypatch.delenv('SANC_API_BASE_URL', raising=False)
    token = 'test.eyJyb2xlIjoiYWRtaW4ifQ.test'
    monkeypatch.setenv('SANC_API_TOKEN', token)
    backend = Backend()
    assert backend.base == 'https://sanc-logistics-fe.vercel.app'
    http = AsyncMock()
    assert asyncio.run(backend.login(http)) == {'Authorization': 'Bearer ' + token}
    http.post.assert_not_called()


def test_api_partner_lookup_and_change_before_confirmation(live, monkeypatch):
    state, api = live
    cfg = configuration()
    cfg['partner_source'] = 'api'
    monkeypatch.setenv('ORDER_CONFIG_JSON', json.dumps(cfg))
    main.config.cache_clear()
    api.partners.return_value = copy.deepcopy(cfg['partners'])
    identifier = draft_id()
    assert api.partners.await_count == 1
    api.partners.return_value[0]['address'] = '바뀐 주소'
    r = CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    assert '변경' in message(r)
    api.create.assert_not_called()


def test_partner_endpoint_contract():
    from backend import Backend
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=configuration()['partners'])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            return await Backend().partners(http, {'Authorization': 'Bearer test'})
    assert asyncio.run(run())[0]['id'] == 7
    assert calls[0].url.path == '/api/partners'
    assert calls[0].url.query == b''
    assert calls[0].headers['Authorization'] == 'Bearer test'


def test_nonadmin_token_rejected_before_any_order(monkeypatch):
    from backend import Backend
    monkeypatch.setenv('SANC_API_TOKEN', 'test.eyJyb2xlIjoibWVtYmVyIn0.test')
    with pytest.raises(ValueError):
        asyncio.run(Backend().login(AsyncMock()))


def test_wrong_owner_response_requires_reconciliation(live):
    state, api = live
    identifier = draft_id()
    api.create.return_value = httpx.Response(201, json={'id': 9, 'userId': 456, 'orderNumber': 'SYN0123456789'})
    r = CLIENT.post('/kakao/order', headers=HEADERS, json=data('확정 '+identifier))
    assert '확인이 필요' in message(r)
    assert state.rows[identifier]['status'] == 'unknown'
