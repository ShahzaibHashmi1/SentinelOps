-- SentinelOps database "shop". Runs once, when the postgres data volume is empty
-- (docker-entrypoint-initdb.d). To re-run it: docker compose down -v

CREATE TABLE items (
    sku         TEXT PRIMARY KEY,
    name        TEXT          NOT NULL,
    price       NUMERIC(10,2) NOT NULL CHECK (price >= 0),
    stock       INTEGER       NOT NULL CHECK (stock >= 0),   -- never goes negative
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sku         TEXT          NOT NULL REFERENCES items (sku),
    quantity    INTEGER       NOT NULL CHECK (quantity > 0),
    customer_id TEXT          NOT NULL,
    amount      NUMERIC(10,2) NOT NULL,
    status      TEXT          NOT NULL CHECK (status IN ('PENDING', 'PAID', 'FAILED')),
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE payments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id        UUID          NOT NULL,   -- no foreign key: payments stays independent of orders
    amount          NUMERIC(10,2) NOT NULL,
    idempotency_key TEXT          NOT NULL UNIQUE,
    status          TEXT          NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Seed: 50 products SKU-001 .. SKU-050, stock 1000, deterministic prices 6.99 .. 91.99
INSERT INTO items (sku, name, price, stock)
SELECT 'SKU-' || lpad(i::text, 3, '0'),
       'Product ' || lpad(i::text, 3, '0'),
       5 + ((i * 7) % 90) + 0.99,
       1000
FROM generate_series(1, 50) AS i;
