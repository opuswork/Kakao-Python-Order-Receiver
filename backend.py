"""Existing SANC auth/catalog/order contract. Never automatically retry order POST."""
import math
import os
import base64
import json
import httpx


class Backend:
    def __init__(self):
        self.base = os.getenv('SANC_API_BASE_URL', 'https://sanc-logistics-fe.vercel.app').rstrip('/')
        if not self.base.startswith('https://'):
            raise ValueError('SANC_API_BASE_URL must use HTTPS')

    async def login(self, client):
        token = os.getenv('SANC_API_TOKEN', '').strip()
        if token:
            # Configuration sanity check only; the backend verifies the signature.
            # Its service overrides userId for non-admins, so fail before any write.
            try:
                segment = token.split('.')[1]
                claims = json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))
                if str(claims.get('role', '')).lower() != 'admin':
                    raise ValueError()
            except Exception:
                raise ValueError('An admin JWT is required for delegated orders') from None
            return {'Authorization': 'Bearer ' + token}
        r = await client.post(self.base + '/api/auth/login', json={
            'username': os.environ['SANC_LOGIN_ID'], 'password': os.environ['SANC_LOGIN_PW']})
        r.raise_for_status()
        data = r.json()
        # Multi-member ordering requires an admin automation account, matching the original script.
        if str(data.get('user', {}).get('role', '')).lower() != 'admin':
            raise ValueError('Use an authorized admin automation account')
        return {'Authorization': 'Bearer ' + data['accessToken']}

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

    async def create(self, client, headers, payload):
        return await client.post(self.base + '/api/orders', headers=headers, json=payload)

    async def partners(self, client, headers):
        # No q: the supplied endpoint limits shared name searches to 30 hits.
        # An authorized admin token returns the full list. Never write Partner data here.
        r = await client.get(self.base + '/api/partners', headers=headers)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise ValueError('Expected Partner array')
        return rows


def client():
    return httpx.AsyncClient(timeout=httpx.Timeout(2.5, connect=1.5), follow_redirects=False, trust_env=False)
