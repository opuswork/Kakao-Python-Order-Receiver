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
