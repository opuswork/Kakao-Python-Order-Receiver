"""PostgreSQL durability and atomic single submission claim. No schema auto-migration."""
import copy
import os
import time

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SESSION_MINUTES = 30


class Store:
    async def connect(self):
        return await psycopg.AsyncConnection.connect(
            os.environ['RECEIVER_DATABASE_URL'], autocommit=True, row_factory=dict_row,
            connect_timeout=2, options='-c statement_timeout=1500')

    async def draft(self, owner, fingerprint, payload, preview, identifier):
        async with await self.connect() as conn:
            await conn.execute('''INSERT INTO kakao_receiver_orders
                (id, owner, fingerprint, payload, preview) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (fingerprint) DO UPDATE SET
                  id=EXCLUDED.id, payload=EXCLUDED.payload, preview=EXCLUDED.preview,
                  status='draft', order_no=NULL, created_at=now(), updated_at=now(),
                  expires_at=now()+interval '15 minutes'
                WHERE kakao_receiver_orders.status='cancelled' ''',
                (identifier, owner, fingerprint, Jsonb(payload), preview))
            cur = await conn.execute('SELECT *, expires_at>now() AS fresh FROM kakao_receiver_orders WHERE fingerprint=%s AND owner=%s',
                                     (fingerprint, owner))
            return await cur.fetchone()

    async def get(self, identifier, owner):
        async with await self.connect() as conn:
            cur = await conn.execute('SELECT *, expires_at > now() AS fresh FROM kakao_receiver_orders WHERE id=%s AND owner=%s',
                                     (identifier, owner))
            return await cur.fetchone()

    async def claim(self, identifier, owner):
        async with await self.connect() as conn:
            cur = await conn.execute('''UPDATE kakao_receiver_orders SET status='posting', updated_at=now()
                WHERE id=%s AND owner=%s AND status='draft' AND expires_at>now() RETURNING *''', (identifier, owner))
            return await cur.fetchone()

    async def finish(self, identifier, owner, status, order_no=None):
        async with await self.connect() as conn:
            await conn.execute('''UPDATE kakao_receiver_orders SET status=%s, order_no=%s, updated_at=now()
                WHERE id=%s AND owner=%s AND status='posting' ''', (status, order_no, identifier, owner))

    async def cancel(self, identifier, owner):
        async with await self.connect() as conn:
            cur = await conn.execute('''UPDATE kakao_receiver_orders SET status='cancelled', updated_at=now()
                WHERE id=%s AND owner=%s AND status='draft' RETURNING id''', (identifier, owner))
            return await cur.fetchone()

    async def session(self, owner):
        async with await self.connect() as conn:
            cur = await conn.execute(f'''SELECT state FROM kakao_receiver_sessions
                WHERE owner=%s AND updated_at > now() - interval '{SESSION_MINUTES} minutes' ''', (owner,))
            row = await cur.fetchone()
            return row['state'] if row else None

    async def save_session(self, owner, state):
        async with await self.connect() as conn:
            await conn.execute('''INSERT INTO kakao_receiver_sessions (owner, state) VALUES (%s,%s)
                ON CONFLICT (owner) DO UPDATE SET state=EXCLUDED.state, updated_at=now()''', (owner, Jsonb(state)))

    async def clear_session(self, owner):
        async with await self.connect() as conn:
            await conn.execute('DELETE FROM kakao_receiver_sessions WHERE owner=%s', (owner,))

    async def profile(self, owner):
        async with await self.connect() as conn:
            cur = await conn.execute('SELECT name, phone FROM kakao_receiver_profiles WHERE owner=%s', (owner,))
            return await cur.fetchone()

    async def save_profile(self, owner, name, phone):
        async with await self.connect() as conn:
            await conn.execute('''INSERT INTO kakao_receiver_profiles (owner, name, phone) VALUES (%s,%s,%s)
                ON CONFLICT (owner) DO UPDATE SET name=EXCLUDED.name, phone=EXCLUDED.phone, updated_at=now()''',
                               (owner, name, phone))

    async def clear_profile(self, owner):
        async with await self.connect() as conn:
            await conn.execute('DELETE FROM kakao_receiver_profiles WHERE owner=%s', (owner,))


class MemoryStore:
    """Same interface in process memory: tests and DRY_RUN without a database only.

    Serverless instances do not share memory, so a conversation may restart between messages.
    """
    def __init__(self):
        self.rows, self.sessions, self.profiles = {}, {}, {}

    async def draft(self, owner, fingerprint, payload, preview, identifier):
        old = next((r for r in self.rows.values() if r['fingerprint'] == fingerprint), None)
        if old and old['status'] != 'cancelled':
            return copy.deepcopy(old)
        row = dict(id=identifier, owner=owner, fingerprint=fingerprint, payload=payload, preview=preview,
                   status='draft', order_no=None, fresh=True)
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
        row = self.rows.get(identifier)
        if row and row['owner'] == owner and row['status'] == 'posting':
            row.update(status=status, order_no=order_no)

    async def cancel(self, identifier, owner):
        row = self.rows.get(identifier)
        if row and row['owner'] == owner and row['status'] == 'draft':
            row['status'] = 'cancelled'
            return {'id': identifier}

    async def session(self, owner):
        saved = self.sessions.get(owner)
        if saved and time.time() - saved[0] < SESSION_MINUTES * 60:
            return copy.deepcopy(saved[1])

    async def save_session(self, owner, state):
        self.sessions[owner] = (time.time(), copy.deepcopy(state))

    async def clear_session(self, owner):
        self.sessions.pop(owner, None)

    async def profile(self, owner):
        return copy.deepcopy(self.profiles.get(owner))

    async def save_profile(self, owner, name, phone):
        self.profiles[owner] = {'name': name, 'phone': phone}

    async def clear_profile(self, owner):
        self.profiles.pop(owner, None)
