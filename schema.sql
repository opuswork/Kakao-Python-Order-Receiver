-- Run once, manually, in the receiver's PostgreSQL database.
-- Does not modify the logistics order tables.
CREATE TABLE IF NOT EXISTS kakao_receiver_orders (
    id text PRIMARY KEY,
    owner text NOT NULL,
    fingerprint text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    preview text NOT NULL,
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','posting','submitted','unknown','rejected','cancelled')),
    order_no text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '15 minutes'
);

-- Chat order form in progress, one per Kakao user. Expired rows (30 min) are ignored.
CREATE TABLE IF NOT EXISTS kakao_receiver_sessions (
    owner text PRIMARY KEY,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The orderer a Kakao user entered first; offered as "주문자는 ○○ 님인가요?" next time.
CREATE TABLE IF NOT EXISTS kakao_receiver_profiles (
    owner text PRIMARY KEY,
    name text NOT NULL,
    phone text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Cached admin JWT from SANC_LOGIN_ID/PW, so the receiver logs in (and the backend announces it) rarely.
CREATE TABLE IF NOT EXISTS kakao_receiver_auth (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    token text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
