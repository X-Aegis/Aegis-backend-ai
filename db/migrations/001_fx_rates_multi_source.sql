-- db/migrations/001_fx_rates_multi_source.sql
--
-- Live FX ingestion (BK-1) writes the same pair from several feeds — the
-- official vendor APIs and the parallel-market feed — which collide under the
-- old ("timestamp", pair) primary key and get silently dropped by the
-- ON CONFLICT DO NOTHING insert. Widen the key to include source and add the
-- indexes used by GET /fx/current, GET /fx/sources and GET /risk/current.
--
-- Safe to run on an existing database; idempotent.

BEGIN;

ALTER TABLE fx_rates DROP CONSTRAINT IF EXISTS fx_rates_pkey;

ALTER TABLE fx_rates
    ADD CONSTRAINT fx_rates_pkey PRIMARY KEY ("timestamp", pair, source);

CREATE INDEX IF NOT EXISTS idx_fx_rates_pair_timestamp
ON fx_rates (pair, "timestamp" DESC);

CREATE INDEX IF NOT EXISTS idx_fx_rates_pair_source_timestamp
ON fx_rates (pair, source, "timestamp" DESC);

COMMIT;
