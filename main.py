"""Kakao skill receiver. Default: dry run, no logistics writes."""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

import dialog
from backend import Backend, ConfigError, client
from contract import build_order
from orders import KST, OrderError, extract
from storage import MemoryStore, Store

app = FastAPI(title='Kakao Order Receiver', docs_url=None, redoc_url=None, openapi_url=None)
logger = logging.getLogger('receiver')
store = Store()
memory = MemoryStore()


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


def storage():
    if os.getenv('RECEIVER_DATABASE_URL'):
        return store
    if dry_run():
        return memory
    raise ValueError('Missing persistent database')


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
        return reply('진행 중인 주문이 없습니다. 주문 내용을 보내 주세요.\n' + dialog.EXAMPLE)
    identifier = row['id']
    status = row['status']
    if status == 'submitted':
        return reply(f"주문이 접수되었습니다.\n주문번호: {row['order_no']}\n{row['preview']}")
    if status == 'draft':
        if row.get('fresh') is False:
            return reply('확인 시간이 만료되었습니다. 취소 후 주문 내용을 다시 보내 주세요.',
                         [('주문 취소', '주문 취소')])
        mode = '[테스트 모드] 확정해도 주문앱에는 보내지 않습니다.\n' if dry_run() else ''
        return reply(mode + '아래 내용을 확인하고 주문 확정을 눌러 주세요. 아직 접수되지 않았습니다.\n' + row['preview'],
                     [('주문 확정', '주문 확정'), ('취소', '주문 취소')])
    if status == 'cancelled':
        return reply('주문 초안이 취소되었습니다. 다시 주문 내용을 입력할 수 있습니다.')
    if status == 'rejected':
        return reply('주문 API가 요청을 거절했습니다. 담당자에게 문의해 주세요.')
    # The confirmation ID stays out of the chat; staff match it via the order memo 카카오접수:<id> and the log.
    return reply('접수 결과 확인이 필요합니다. 중복 접수를 막기 위해 재전송하지 않습니다. 담당자에게 실제 주문 목록 확인을 요청해 주세요.',
                 [('결과 확인', '결과 확인')])


class Context:
    """Backend lookups for dialog.py. Connects and logs in at most once, and only when needed."""
    def __init__(self, profile, today, cache):
        self.api, self.client, self.headers = Backend(), None, None
        self.profile, self.today, self.cache = profile, today, cache

    @property
    def http(self):
        if self.client is None:
            self.client = client()
        return self.client

    async def close(self):
        if self.client is not None:
            await self.client.aclose()

    async def auth(self):
        if self.headers is None:
            self.headers = await self.api.login(self.http, self.cache)
        return self.headers

    async def catalog(self):
        return await self.api.catalog(self.http, await self.auth())

    async def members(self, name):
        return await self.api.members(self.http, await self.auth(), name)

    async def churches(self):
        return await self.api.churches(self.http)

    async def partners(self, q):
        return await self.api.partners(self.http, await self.auth(), q)


async def confirm(st, owner, action, identifier):
    row = await st.get(identifier, owner)
    if not row:
        return row_reply(None)
    if action == '취소':
        await st.cancel(identifier, owner)
        return row_reply(await st.get(identifier, owner))
    if action == '조회' or row['status'] != 'draft' or not row.get('fresh', True):
        return row_reply(row)
    if dry_run():
        await st.cancel(identifier, owner)
        return reply('[테스트 모드] 주문앱에는 보내지 않았습니다.\n' + row['preview'])
    api = Backend()
    async with client() as http:
        headers = await api.login(http, st)  # Login failure occurs before the claim.
        claimed = await st.claim(identifier, owner)
        if not claimed:
            return row_reply(await st.get(identifier, owner))
        request = claimed['payload']['request']
        # From this point, any cancellation/error leaves status posting/unknown.
        # Never retry a possibly committed POST without backend idempotency support.
        try:
            response = await api.create(http, headers, request)
            if response.status_code in (200, 201):
                result = response.json()
                # Do not turn arbitrary 2xx/202 acknowledgments into order success.
                number = result.get('orderNumber') if isinstance(result, dict) else None
                owner_id = result.get('userId') if isinstance(result, dict) else None
                # A new orderer gets a member id the receiver could not know in advance.
                owner_ok = (type(owner_id) is int and owner_id > 0 if 'ordererProfile' in request
                            else owner_id == request['userId'])
                if (not isinstance(number, str) or not number.strip()
                        or type(result.get('id')) is not int or result['id'] <= 0 or not owner_ok):
                    raise ValueError('Unconfirmed order response')
                await st.finish(identifier, owner, 'submitted', number)
                claimed.update(status='submitted', order_no=number)
                return row_reply(claimed)
            if response.status_code == 401:
                await st.clear_token()
            status = 'rejected' if 400 <= response.status_code < 500 else 'unknown'
            logger.error('order_not_confirmed id=%s status=%s http=%s', identifier, status, response.status_code)
            await st.finish(identifier, owner, status)
            return row_reply({**claimed, 'status': status})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error('order_not_confirmed id=%s status=unknown code=%s', identifier, error_code(exc))
            await st.finish(identifier, owner, 'unknown')
            return row_reply({**claimed, 'status': 'unknown'})


async def process(body):
    text = body.userRequest.utterance.strip()
    owner = body.userRequest.user.id
    if text == '연결확인':
        return reply('카카오 → Python 서버 연결 성공.\n모드: ' + ('테스트(주문 저장 안 함)' if dry_run() else '실제 주문'))
    if text == '내 연결코드':
        return reply('담당자에게 아래 연결코드를 전달해 주세요.\n' + owner)
    st = storage()
    cmd = re.fullmatch(r'(확정|취소|조회) ([0-9a-f]{32})', text)  # Buttons sent before IDs were hidden.
    if cmd:
        return await confirm(st, owner, *cmd.groups())
    if text in ('주문 확정', '결과 확인'):
        latest = await st.latest(owner)
        if not latest:
            return row_reply(None)
        return await confirm(st, owner, '확정' if text == '주문 확정' else '조회', latest['id'])
    if text in ('주문자 변경', '내 정보 삭제'):
        await st.clear_profile(owner)
        await st.clear_session(owner)
        return reply('저장된 주문자 정보를 지웠습니다. 다음 주문 때 다시 여쭤볼게요.')
    if text in dialog.CANCEL_WORDS:
        await st.clear_session(owner)
        latest = await st.latest(owner)
        if latest and latest['status'] == 'draft':
            return await confirm(st, owner, '취소', latest['id'])
        return reply('주문 입력을 취소했습니다. 새 주문 내용을 보내 주세요.\n' + dialog.EXAMPLE)
    today = dt.datetime.now(KST).date()
    s = await st.session(owner)
    try:
        found = extract(text, today)
    except OrderError:
        if s is None:
            raise
        found = None  # A reply to a question need not parse as an order.
    if s and found and found['items'] and found.get('date'):
        s = None  # A complete new order replaces the one in progress.
    ctx = Context(await st.profile(owner), today, st)
    try:
        problem = ''
        if s is None:
            if not dialog.is_order_like(found):
                return reply('주문 내용을 보내 주세요. 날짜, 상품과 수량, 받는 곳을 쉼표나 줄바꿈으로 나눠 적으면 됩니다.\n'
                             + dialog.EXAMPLE)
            s = dialog.new_state(found)
        else:
            try:
                await dialog.answer(s, text, ctx)
            except OrderError as exc:
                problem = str(exc) + '\n\n'
        question = await dialog.next_question(s, ctx)
        if question:
            await st.save_session(owner, s)
            return reply(problem + question.text, question.choices)
        await ctx.auth()
        payload, preview = build_order(s, ctx.api.admin_id, today)
    finally:
        await ctx.close()
    identifier = uuid.uuid4().hex
    payload['extraNote'] = '카카오접수:' + identifier
    canonical = {'owner': owner, 'phone': s['orderer']['phone'], 'date': s['date'], 'company': s['company'],
                 'kind': s['kind'], 'items': sorted((i['productName'], i['quantity']) for i in payload['items'])}
    fingerprint = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if s.get('save_profile'):
        await st.save_profile(owner, s['orderer']['name'], s['orderer']['phone'])
    row = await st.draft(owner, fingerprint, {'request': payload}, preview, identifier)
    await st.clear_session(owner)
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
        code = error_code(exc)
        logger.error('receiver_failure code=%s', code)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
            await forget_token()
        return reply('처리 중 오류가 발생했습니다. 접수 완료로 처리하지 않았습니다. 확정 중이었다면 담당자에게 실제 주문 목록 확인을 요청해 주세요.'
                     f'\n(오류 코드: {code})')


async def forget_token():
    """A rejected cached token (e.g. rotated JWT secret) must not be reused; the next message logs in."""
    try:
        await storage().clear_token()
    except Exception:
        logger.error('receiver_failure code=token_clear')


def error_code(exc):
    """Short, secret-free cause shown to the user so failures can be diagnosed without server logs."""
    if isinstance(exc, ConfigError):
        return f'설정 오류: {exc}'
    if isinstance(exc, httpx.HTTPStatusError):
        return f'{type(exc).__name__} {exc.response.status_code} {exc.request.url.path}'
    if isinstance(exc, httpx.RequestError):
        try:
            return f'{type(exc).__name__} {exc.request.url.path}'
        except RuntimeError:  # No request attached.
            pass
    return type(exc).__name__
