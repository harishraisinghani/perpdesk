-- Exact live hot path, computed inside Lakebase. The FastAPI production repository reads this
-- view; Spark is not in the request path. The same equations live in perpdesk/shocks.py and are
-- compared against a brute-force oracle in tests.
CREATE OR REPLACE VIEW live_account_joint_liq_px AS
WITH tier_rates AS (
  SELECT
    margin_table_id,
    tier,
    lower_bound,
    lead(lower_bound) OVER (PARTITION BY margin_table_id ORDER BY tier) AS next_lower_bound,
    1::numeric / (2 * max_leverage) AS mmr,
    lag(1::numeric / (2 * max_leverage))
      OVER (PARTITION BY margin_table_id ORDER BY tier) AS previous_mmr
  FROM meta_margin_tables
),
tier_derived AS (
  SELECT
    *,
    sum(
      CASE WHEN previous_mmr IS NULL THEN 0
           ELSE lower_bound * (mmr - previous_mmr) END
    ) OVER (
      PARTITION BY margin_table_id ORDER BY tier
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS deduction
  FROM tier_rates
),
position_now AS (
  SELECT
    p.account,
    p.coin,
    p.szi,
    c.mark_px,
    p.szi * c.mark_px AS signed_notional,
    abs(p.szi * c.mark_px) AS notional_now,
    p.liquidation_px_reported,
    u.margin_table_id,
    current_tier.mmr,
    current_tier.deduction,
    abs(p.szi * c.mark_px) * current_tier.mmr - current_tier.deduction AS mm_now
  FROM positions_current p
  JOIN accounts_current tracked
    ON tracked.account = p.account AND tracked.tier IN (0, 1)
  JOIN asset_ctx_current c USING (coin)
  JOIN meta_universe u USING (coin)
  JOIN LATERAL (
    SELECT t.mmr, t.deduction
    FROM tier_derived t
    WHERE t.margin_table_id = u.margin_table_id
      AND t.lower_bound <= abs(p.szi * c.mark_px)
    ORDER BY t.tier DESC
    LIMIT 1
  ) current_tier ON true
  WHERE p.leverage_type = 'cross' AND p.szi <> 0
),
account_now AS (
  SELECT
    p.account,
    a.total_raw_usd + sum(p.signed_notional) AS av_now,
    sum(p.mm_now) AS mm_now
  FROM position_now p
  JOIN accounts_current a USING (account)
  GROUP BY p.account, a.total_raw_usd
),
candidate_equations AS (
  SELECT
    p.*,
    a.av_now,
    a.mm_now AS account_mm_now,
    t.lower_bound,
    t.next_lower_bound,
    -(
      a.av_now - p.signed_notional - (a.mm_now - p.mm_now) + t.deduction
    ) / nullif(p.signed_notional - p.notional_now * t.mmr, 0) AS root
  FROM position_now p
  JOIN account_now a USING (account)
  JOIN tier_derived t USING (margin_table_id)
),
valid_roots AS (
  SELECT
    *,
    row_number() OVER (
      PARTITION BY account, coin ORDER BY abs(root - 1)
    ) AS root_rank
  FROM candidate_equations
  WHERE root >= lower_bound / nullif(notional_now, 0)
    AND (next_lower_bound IS NULL OR root <= next_lower_bound / nullif(notional_now, 0))
    AND (
      (szi > 0 AND root BETWEEN 0 AND 1)
      OR (szi < 0 AND root BETWEEN 1 AND 5)
    )
)
SELECT
  account,
  coin,
  CASE WHEN szi > 0 THEN 'down' ELSE 'up' END AS direction,
  root AS joint_liq_multiplier,
  root * mark_px AS joint_liq_px,
  CASE WHEN liquidation_px_reported IS NULL THEN NULL
       ELSE liquidation_px_reported / mark_px END AS marginal_liq_multiplier,
  notional_now,
  now() AS computed_at
FROM valid_roots
WHERE root_rank = 1;

COMMENT ON VIEW live_account_joint_liq_px IS
  'Exact live cross-account liquidation roots at current Lakebase marks; no Spark in the request path.';
