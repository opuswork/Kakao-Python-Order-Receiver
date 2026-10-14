"""Kakao skill receiver. Default: dry run, no logistics writes."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from backend import Backend, client
from contract import build_order
from orders import OrderError, parse_order, summary
from storage import Store

app = FastAPI(title='Kakao Order Receiver', docs_url=None, redoc_url=None, openapi_url=None)
logger = logging.getLogger('receiver')
store = Store()


async def partner_rows(api, http, headers, cfg):
    source = cfg.get('partner_source', 'api')
    if source == 'api':
        return await api.partners(http, headers)
    if source == 'config':
        return cfg.get('partners', [])
    raise ValueError('partner_source must be api or config')


class User(BaseModel):
    id: str = Field(min_length=1, max_length=200)


class UserRequest(BaseModel):
    utterance: str = Field(min_length=1, max_length=4000)
    user: User


class SkillRequest(BaseModel):
    userRequest: UserRequest


def reply(text, choices=None):
    template = {'outputs': [{'simpleText': {'text': text}}]}
    if choices:
        template['quickReplies'] = [{'label': label, 'action': 'message', 'messageText': message}
                                    for label, message in choices]
    return {'version': '2.0', 'template': template}


def dry_run():
    value = os.getenv('DRY_RUN', 'true').lower()
    if value not in ('true', 'false'):
        raise ValueError('DRY_RUN must be true or false')
    return value == 'true'


@lru_cache
def config():
    # Runtime environment variable, never customer details committed to Git.
    return json.loads(os.getenv('ORDER_CONFIG_JSON', '{}'))


def authorize(request):
    secret = os.getenv('KAKAO_SKILL_SECRET', '')
    if len(secret) < 32:
        raise HTTPException(503, 'Configure KAKAO_SKILL_SECRET (32+ characters)')
    given = request.headers.get('X-Kakao-Skill-Key', '')
    if not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(401, 'Unauthorized')


@app.get('/')
@app.get('/health')
async def health():
    return {'status': 'ok', 'service': 'kakao-order-receiver'}


def row_reply(row):
    if not row:
        return reply('주문을 찾지 못했습니다. 본인이 작성한 주문인지 확인해 주세요.')
    identifier = row['id']
    status = row['status']
    if status == 'submitted':
        return reply(f"주문이 접수되었습니다.\n주문번호: {row['order_no']}\n{row['preview']}")
    if status == 'draft':
        if row.get('fresh') is False:
            return reply('확인 시간이 만료되었습니다. 취소 후 주문 내용을 다시 보내 주세요.',
                         [('주문 취소', '취소 ' + identifier)])
        return reply('아래 내용을 확인하고 주문 확정을 눌러 주세요. 아직 접수되지 않았습니다.\n' + row['preview'],
                     [('주문 확정', '확정 ' + identifier), ('취소', '취소 ' + identifier)])
    if status == 'cancelled':
        return reply('주문 초안이 취소되었습니다. 다시 주문 내용을 입력할 수 있습니다.')
    if status == 'rejected':
        return reply('주문 API가 요청을 거절했습니다. 담당자에게 문의해 주세요.\n확인번호: ' + identifier)
    return reply('접수 결과 확인이 필요합니다. 중복 접수를 막기 위해 재전송하지 않습니다. 담당자에게 확인번호를 알려 주세요.\n확인번호: ' + identifier,
                 [('결과 확인', '조회 ' + identifier)])


async def process(body):
    text = body.userRequest.utterance.strip()
    owner = body.userRequest.user.id
    if text == '연결확인':
        return reply('카카오 → Python 서버 연결 성공.\n모드: ' + ('테스트(주문 저장 안 함)' if dry_run() else '실제 주문'))
    if text == '내 연결코드':
        return reply('담당자에게 아래 연결코드를 전달해 주세요.\n' + owner)
    if dry_run():
        parsed = parse_order(text)
        return reply('[테스트] 분석 결과입니다. 실제 주문은 저장하지 않았습니다.\n' + summary(parsed))
    cfg = config()
    binding = cfg.get('bindings', {}).get(owner)
    if not binding:
        return reply('조합원 계정 연결이 필요합니다. “내 연결코드”를 입력하고 담당자에게 전달해 주세요.')
    if not os.getenv('RECEIVER_DATABASE_URL'):
        raise ValueError('Missing persistent database')
    cmd = re.fullmatch(r'(확정|취소|조회) ([0-9a-f]{32})', text)
    if cmd:
        action, identifier = cmd.groups()
        row = await store.get(identifier, owner)
        if not row:
            return row_reply(None)
        if action == '취소':
            await store.cancel(identifier, owner)
            return row_reply(await store.get(identifier, owner))
        if action == '조회' or row['status'] != 'draft' or not row.get('fresh', True):
            return row_reply(row)
        # Re-check current member/destination authorization before creating an order.
        saved = row['payload']
        if ('request' not in saved or saved['request']['userId'] != binding.get('userId')
                or saved.get('partnerId') not in binding.get('allowed_partner_ids', [])):
            raise OrderError('주문 권한 설정이 변경되었습니다. 담당자에게 확인해 주세요.')
        api = Backend()
        async with client() as http:
            headers = await api.login(http)  # Login failure occurs before the claim.
            current_partners = await partner_rows(api, http, headers, cfg)
            matches = [p for p in current_partners if p.get('id') == saved['partnerId']]
            if (len(matches) != 1 or saved.get('partnerSnapshot') !=
                    {k: matches[0].get(k) for k in ('name', 'contactName', 'phone', 'address')}):
                raise OrderError('거래처 정보가 변경되었습니다. 초안을 취소하고 다시 입력해 주세요.')
            claimed = await store.claim(identifier, owner)
            if not claimed:
                return row_reply(await store.get(identifier, owner))
            # From this point, any cancellation/error leaves status posting/unknown.
            # Never retry a possibly committed POST without backend idempotency support.
            try:
                response = await api.create(http, headers, claimed['payload']['request'])
                if response.status_code in (200, 201):
                    result = response.json()
                    # Do not turn arbitrary 2xx/202 acknowledgments into order success.
                    number = result.get('orderNumber') if isinstance(result, dict) else None
                    if (not isinstance(number, str) or not number.strip()
                            or type(result.get('id')) is not int or result['id'] <= 0
                            or result.get('userId') != claimed['payload']['request']['userId']):
                        raise ValueError('Unconfirmed order response')
                    await store.finish(identifier, owner, 'submitted', number)
                    claimed.update(status='submitted', order_no=number)
                    return row_reply(claimed)
                status = 'rejected' if 400 <= response.status_code < 500 else 'unknown'
                await store.finish(identifier, owner, status)
                return row_reply({**claimed, 'status': status})
            except asyncio.CancelledError:
                raise
            except Exception:
                await store.finish(identifier, owner, 'unknown')
                return row_reply({**claimed, 'status': 'unknown'})

    parsed = parse_order(text)
    api = Backend()
    async with client() as http:
        headers = await api.login(http)
        catalog, partners = await asyncio.gather(api.catalog(http, headers), partner_rows(api, http, headers, cfg))
    payload, preview = build_order(parsed, binding, cfg, catalog, partners)
    identifier = uuid.uuid4().hex
    request_payload = payload['request']
    request_payload['extraNote'] = '카카오접수:' + identifier
    canonical = {'owner': owner, 'userId': request_payload['userId'],
                 'partnerId': payload['partnerId'], 'date': parsed['ship_date'],
                 'items': sorted((i['productName'], i['quantity']) for i in request_payload['items'])}
    fingerprint = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    row = await store.draft(owner, fingerprint, payload, preview, identifier)
    return row_reply(row)


@app.post('/kakao/order')
async def receive(request: Request):
    authorize(request)
    try:
        # Count actual streamed bytes, including requests without Content-Length.
        raw = bytearray()
        async with asyncio.timeout(1):
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 32768:
                    raise HTTPException(413, 'Request too large')
        body = SkillRequest.model_validate_json(raw)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, 'Invalid Kakao skill payload') from None
    try:
        # This bounds in-process work; cold start/network time is additional.
        async with asyncio.timeout(3.5):
            return await process(body)
    except OrderError as exc:
        return reply(str(exc))
    except TimeoutError:
        return reply('처리 시간이 초과되었습니다. 확정 중이었다면 같은 확정 버튼으로 결과를 확인해 주세요. 새 주문으로 다시 입력하지 마세요.')
    except Exception as exc:
        # Do not log access tokens, customer addresses, request bodies or database URLs.
        logger.error('receiver_failure type=%s', type(exc).__name__)
        return reply('처리 중 오류가 발생했습니다. 접수 완료로 처리하지 않았습니다. 확정 중이었다면 담당자에게 실제 주문 목록 확인을 요청해 주세요.')
