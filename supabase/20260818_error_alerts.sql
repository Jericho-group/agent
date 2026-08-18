CREATE TABLE IF NOT EXISTS error_alerts (
  id           bigserial PRIMARY KEY,
  ts           timestamptz DEFAULT now(),
  path         text,
  status_code  int,
  method       text,
  error_hash   text,          -- md5(path+status_code) для группировки
  count        int DEFAULT 1,
  last_body    text,          -- обрезано до 2000 chars
  tg_sent      bool DEFAULT false
);
CREATE INDEX IF NOT EXISTS ix_error_alerts_recent ON error_alerts(ts DESC);
CREATE INDEX IF NOT EXISTS ix_error_alerts_hash ON error_alerts(error_hash, ts DESC);
