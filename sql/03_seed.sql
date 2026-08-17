-- Safe defaults. Discovery cold-starts from the BTC/ETH/SOL/HYPE trades WebSockets; the app can
-- explicitly promote known public addresses without relying on an undocumented leaderboard API.
INSERT INTO collector_config (key, value) VALUES
  ('t0_size', '10'),
  ('t1_size', '1500'),
  ('discovery_retention_days', '30'),
  ('periodic_snapshot_seconds', '900')
ON CONFLICT (key) DO NOTHING;

INSERT INTO poll_cursor (tier) VALUES (1), (2)
ON CONFLICT (tier) DO NOTHING;

-- Optional manually verified seeds:
-- INSERT INTO accounts_discovered (address, promoted) VALUES ('0x...', true)
-- ON CONFLICT (address) DO UPDATE SET promoted = true;
