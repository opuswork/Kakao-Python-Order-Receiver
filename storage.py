"""PostgreSQL durability and atomic single submission claim. No schema auto-migration."""
import os
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


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
