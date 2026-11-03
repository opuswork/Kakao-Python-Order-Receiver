"""Existing SANC auth/catalog/order contract. Never automatically retry order POST."""
import math
import os
import base64
import json
import time

import httpx

# Tokens live 24h (JWT_EXPIRES_SECONDS). Refresh a cached one in its last 6 hours.
REFRESH_BEFORE = 6 * 3600


class ConfigError(RuntimeError):
    """Missing deployment setting. The message is safe to show to users (no values)."""


def claims(token):
    """JWT payload without verification; the backend verifies the signature."""
    segment = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))


class Backend:
    def __init__(self):
        self.base = os.getenv('SANC_API_BASE_URL', 'https://be-aosm.onrender.com').rstrip('/')
        if not self.base.startswith('https://'):
            raise ValueError('SANC_API_BASE_URL must use HTTPS')
        self.admin_id = None

    async def login(self, client, cache=None):
        """Admin headers. cache (storage) keeps the JWT so each message does not log in again:
        every backend login posts an announcement to the admin live chat."""
        token = os.getenv('SANC_API_TOKEN', '').strip()
        if token:
            # Configuration sanity check only.
            # Its service overrides userId for non-admins, so fail before any write.
            try:
                payload = claims(token)
                if str(payload.get('role', '')).lower() != 'admin':
                    raise ValueError()
                self.admin_id = int(payload['sub'])
            except Exception:
                raise ConfigError('SANC_API_TOKEN이 관리자 토큰이 아님') from None
            return {'Authorization': 'Bearer ' + token}
        cached = await cache.token() if cache is not None else None
        if cached:
            try:
                payload = claims(cached)
                left = float(payload['exp']) - time.time()
                admin_id = int(payload['sub'])
            except Exception:
                left = 0
            if left > 60:
                self.admin_id = admin_id
                if left < REFRESH_BEFORE:
                    cached = await self.refresh(client, cached, cache)
                return {'Authorization': 'Bearer ' + cached}
        login_id, password = os.getenv('SANC_LOGIN_ID', ''), os.getenv('SANC_LOGIN_PW', '')
        if not (login_id and password):
            raise ConfigError('SANC_API_TOKEN 없음')
        r = await client.post(self.base + '/api/auth/login', json={'username': login_id, 'password': password})
        r.raise_for_status()
        data = r.json()
        # Ordering for members requires an admin automation account, matching the original script.
        if str(data.get('user', {}).get('role', '')).lower() != 'admin':
            raise ValueError('Use an authorized admin automation account')
        self.admin_id = int(data['user']['id'])
        if cache is not None:
            await cache.save_token(data['accessToken'])
        return {'Authorization': 'Bearer ' + data['accessToken']}

    async def refresh(self, client, token, cache):
        """Sliding renewal (POST /api/auth/refresh). Best effort: the old token is still valid."""
        try:
            r = await client.post(self.base + '/api/auth/refresh', headers={'Authorization': 'Bearer ' + token})
            r.raise_for_status()
            renewed = r.json()['accessToken']
            claims(renewed)
        except Exception:
            return token
        await cache.save_token(renewed)
        return renewed

    async def catalog(self, client, headers):
        r = await client.get(self.base + '/api/stock-inventory', headers=headers)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get('items') or data.get('data') or []
        if not rows:
            raise ValueError('Empty catalog')
        for row in rows:
            price = row.get('wholesalePrice')
            if price is not None and (not math.isfinite(float(price)) or float(price) < 0):
                raise ValueError('Invalid catalog price')
        return rows

    async def members(self, client, headers, name):
        # Admin autocomplete: fullname contains q, at most 10 rows.
        r = await client.get(self.base + '/api/members/search', headers=headers, params={'q': name})
        r.raise_for_status()
        return r.json()

    async def churches(self, client):
        r = await client.get(self.base + '/api/churches')
        r.raise_for_status()
        return r.json()

    async def partners(self, client, headers, q):
        # Name contains q, at most 30 rows. Read only; never write Partner data here.
        r = await client.get(self.base + '/api/partners', headers=headers, params={'q': q})
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise ValueError('Expected Partner array')
        return rows

    async def create(self, client, headers, payload):
        return await client.post(self.base + '/api/orders', headers=headers, json=payload)


def client():
    return httpx.AsyncClient(timeout=httpx.Timeout(2.5, connect=1.5), follow_redirects=False, trust_env=False)
