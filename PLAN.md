# PerpDesk v3 — implementation plan

Derived from the v3 spec (`.context/attachments/J6Gyc3/`) and the ChainPulse build guide
(`.context/attachments/1yVitG/`). Every Hyperliquid claim below was verified against mainnet on
2026-08-15 before this plan was written; see §1. ChainPulse conventions are reused verbatim
wherever they apply — noted inline as **[CP §n]**.

---

## 1. Pre-flight: what is already de-risked

Run live against `api.hyperliquid.xyz`. These collapse most of phase 0.

**The reconciliation anchor works.** 40 addresses harvested from `recentTrades` on
BTC/ETH/SOL/HYPE, `clearinghouseState` pulled for each, cross maintenance margin recomputed from
the published formula and compared to the exchange's `crossMaintenanceMarginUsed`:

```
10 of 10 accounts matched. Worst relative error 3.75e-07, most below 1e-09.
```

The formula, confirmed:

```
mmr(k)       = 1 / (2 * max_leverage(k))
ded(0)       = 0
ded(k)       = ded(k-1) + lower_bound(k) * (mmr(k) - mmr(k-1))
mm(notional) = notional * mmr(k) - ded(k),  k = highest tier with lower_bound <= notional
```

Sum `mm(|positionValue|)` over **cross positions only**. The spec's `0.001` tolerance is ~4 orders
of magnitude looser than reality — tighten to `1e-6` so the expectation can actually fail.

**The account-value identity holds** (this one carries §4):

```
accountValue = totalRawUsd + Σ_c szi_c * markPx_c
```

Checked on 8 live accounts. Residuals 0.0000–1.34 absolute, worst 3.5e-05 relative on the largest
book — consistent with mark drift in the seconds between the `metaAndAssetCtxs` call and the
per-account call, not with a modelling error.

**Margin tables confirmed.** 232 coins, 7 tables. BTC id 56 = `40x → 20x at $150M`; ETH id 55 =
`25x → 15x at $100M`. Spec accurate.

**Landmine — 4 of 10 referenced table ids don't exist in `marginTables`:**

```
available: [50, 51, 52, 53, 54, 55, 56]
referenced: [3, 5, 10, 20, 51, 52, 53, 54, 55, 56]
missing:   [3, 5, 10, 20]
```

A bare id `N` means a single untiered tier at `maxLeverage = N` (ATOM: `marginTableId 5`,
`maxLeverage 5`). An inner join on `marginTables` silently drops most of the 232 coins and the
pipeline stays green. Encode the fallback; add an expectation that every universe coin resolves
and that the bottom tier's leverage equals `universe.maxLeverage`.

**`trades` WS carries `users: [buyer, seller]`.** Confirmed live. Discovery works.

**`clearinghouseState` is also a WebSocket subscription** — not in the spec.

```json
{"method":"subscribe","subscription":{"type":"clearinghouseState","user":"0x..."}}
```

Arbitrary addresses, no auth, same payload as REST, pushes every ~4 s (17 messages for 2 accounts
over 35 s ≈ 14.6/min/account). **Zero REST weight.** This rewrites the collector — §3 and §4.

Minor: the spec says `webData2`; current is `webData3`. Not needed here.

---

## 2. Free Edition is the binding constraint

ChainPulse establishes the environment, and it is tighter than the v3 spec assumes.

| Limit | Consequence for PerpDesk |
|---|---|
| **Lakebase CDF cannot start at all** — the `workspace` catalog is Databricks-managed Default Storage, which is an unsupported CDF destination **[CP-rev §5]** | No Postgres→Delta streaming. Foreign catalog + batch materialized views instead. Rewrites §3, §5, §8 |
| Serverless egress may be DNS-blocked | Collector runs on your laptop. Same conclusion as ChainPulse, same reason — and the better architecture to demo anyway |
| **One active pipeline per type** | One Lakeflow pipeline for bronze→silver→gold. A *Job* is a different object and is still available — §8 uses that |
| One Lakebase Autoscaling project | Fine |
| 3 apps, auto-stop 24 h after last start | Restart before recording |

### 2.1 The CDF replacement, and what it costs

Confirmed empirically in the ChainPulse revision, not a permissions or schema failure. The
fallback **[CP-rev §5,6]**:

- Register the Lakebase database as a **read-only Unity Catalog foreign catalog**, `perpdesk_pg`.
  It's a live governed link to Postgres; it stores no Delta.
- Expose a curated Postgres **view** to it rather than raw tables.
- The pipeline reads that view with `spark.read` and materializes into
  `workspace.perpdesk.bronze_*`. No `readStream`, no `create_auto_cdc_flow`, no `_pg_lsn`.
- Delta CDF (`delta.enableChangeDataFeed`) is a *different* feature and still works — it's what
  the Delta→Postgres synced table needs in phase 10.

**What's lost: history.** The federated read returns current Postgres state. There are no
before/after images and no change log. Refresh is manual or scheduled, not ~15 s.

**What this costs PerpDesk is much less than it cost ChainPulse.** ChainPulse's headline claim was
"Postgres changes stream into Delta automatically" — the constraint deletes it. PerpDesk's
headline claim is that the liquidation map is computed exactly and continuously proven against the
exchange's own numbers. That is untouched. CDF was a supporting detail here, not the thesis.

**One class of problem PerpDesk avoids entirely:** the ChainPulse revision spent most of its
phase-3 rework on Infura's free tier — `MAX_SPAN` down to 1 block, credit budgeting, batch
requests abandoned. PerpDesk talks to `api.hyperliquid.xyz` directly. No third-party RPC, no
credit allowance, no provider to degrade around. Worth a line in the write-up.

**Run the egress test first [CP §0.3]** against `api.hyperliquid.xyz` rather than an Ethereum RPC:

```python
import requests
r = requests.post("https://api.hyperliquid.xyz/info", json={"type":"meta"}, timeout=10)
print(r.status_code, len(r.json()["universe"]))
```

`Temporary failure in name resolution` means blocked. Either way build the collector external —
ChainPulse's argument applies unchanged.

**One trap PerpDesk dodges:** ChainPulse needed `numeric(78,0)` + a generated text column because
uint256 exceeds Spark's 38-digit DECIMAL ceiling **[CP §1.5]**. Hyperliquid returns decimal
strings; everything fits `numeric(38,18)` and round-trips to Delta cleanly. Note it in the
write-up as a case where the previous project's workaround wasn't needed — it shows you knew to
check rather than cargo-culting the pattern.

---

## 3. Where the history lives — the decision the CDF loss forces

Under CDF this was a throughput problem. Under federation it's worse and simpler: **the pipeline
re-scans whatever Postgres holds, in full, on every refresh, over a JDBC link.** An append-only
history table is now actively hostile — it grows without bound and every refresh drags all of it
across the wire.

Naive volume, if every state update appends a row:

```
T0  10 accts via WebSocket     =   live current-state updates
T1  990 accts via REST         =   198 base refreshes/min
candidate scoring uses the remaining REST budget up to 1,180 weight/min
```

Re-scanned every refresh. That is where this project dies.

**The design: Postgres holds current state only. History accumulates in Delta.**

That is the right split anyway — it's what each store is for — and the constraint just makes it
mandatory instead of optional.

**Postgres side — bounded, never grows with time:**

- `positions_current` — one row per `(account, coin)`, upserted. ~15k rows at 5k accounts.
- `accounts_current` — one row per account: `total_raw_usd`, reported `account_value`,
  reported `cross_mm`, `book_hash`, `observed_at`. ~5k rows.
- `asset_ctx_current` — one row per coin. 232 rows.

The federated scan is then ~20k rows regardless of how long the collector has been running. That
is a rounding error over JDBC, and it stays that way on day thirty.

**Still hash the book.** `(coin, szi, entry_px, leverage, margin_mode)` per position plus
`total_raw_usd`; skip the write when unchanged. The reason is no longer CDF volume — it's that
the WS pushes every ~4 s because the *mark* moved, not because the account did anything, and
without the hash you'd issue ~2,400 pointless upserts/min against a database the Streamlit app is
also reading. Marks live in their own table and update at 232 rows/min flat.

Account value is recomputed at read time from the §1 identity,
`AV = total_raw_usd + Σ_c szi_c * mark_c`, which is what makes the split legal.

**Delta side — history accumulates where it belongs:**

Each pipeline refresh materializes a bronze snapshot with a `captured_at` column. The map is
recomputed and **appended** to `gold_liquidation_map_history`. That append is a few thousand rows
per refresh (coin × shock bucket), not millions.

Lakeflow materialized views fully recompute, so they can't append. Two ways to get the append,
and Free Edition allows both since a **Job is a different object from a pipeline**:

1. A second task in the same Job running `INSERT INTO gold_liquidation_map_history SELECT * FROM
   gold_liquidation_map` after the pipeline task. Simplest; recommended.
2. A `dp.create_streaming_table` fed by `spark.readStream` over the bronze *Delta* table (Delta
   supports streaming reads even though the federated source doesn't).

Take (1) and say why: it's one SQL statement and the alternative buys nothing here.

**This is also the honest answer to §7.5.** The map history is genuinely a Delta-scale artifact
that Postgres could not hold, and it's what the paper ledger scores against. The architecture now
demonstrates the split rather than asserting it.

**Two things improve as a side effect.** The map is computed with every account marked at the same
current price, so the spec's §9 caveat narrows to "positions as-of last observed, prices live."
And keeping a periodic full refresh — every account every 15 min, hash or no — gives the second
reconciliation expectation: reconstructed `AV` vs the exchange's reported `accountValue`, same
discipline as the maintenance-margin check.

---

## 4. Collector: hybrid push + poll

| Tier | Population | Mechanism | Freshness | REST cost |
|---|---|---|---|---|
| T0 | top 10 by cross notional | `clearinghouseState` WS | ~4 s | 0 |
| T1 | next 990 | REST, 5 min cycle | 5 min | 198 req/min |
| T2 | tail, round-robin cursor | REST, 15–30 min | 15–30 min | 200 req/min |
| — | `metaAndAssetCtxs` | REST, 1 min | 1 min | weight 20 |
| — | `meta` | REST, hourly | 1 h | weight 20 |
| — | discovery | `trades` WS | push | 0 |

Budget: 1200 weight/min/IP; `clearinghouseState` = 2, other info calls = 20.
`(300+200)*2 + 20 = 1020`, ~15% headroom. 500 sequential HTTPS round-trips won't fit in 60 s —
4–6 concurrent workers behind one shared token bucket.

T0 is capped at 10 because Hyperliquid permits at most 10 unique users across user-specific
WebSocket subscriptions per IP. The broader 1,000-subscription and 2,000-sent-message limits do
not raise that user-specific ceiling. The remaining 990 accounts fit comfortably in a five-minute
REST rotation because `clearinghouseState` costs weight 2.

Why the hybrid matters beyond speed: position size is power-law distributed, so T0 holds a large
share of tracked notional. That is the honest answer to the spec's own "snapshot, not simulation"
limitation — for the accounts carrying the mass it is live at 4 seconds. Publish
`share_of_tracked_notional_live` next to the coverage number.

**Structure, following ChainPulse [CP §3.4] exactly:** `cycle()` does one pass and returns;
`main()` owns the loop; `pool`, `ws`, and caches are created in `main()` and passed in. Same
reasons — single-shot mode for a future Lakeflow job or GH Actions cron, testability, and not
re-minting OAuth every cycle. `collector/db.py` is ChainPulse's `tailer/db.py` **unchanged**,
including the `OAuthConnection` subclass that mints a fresh credential per connection.

**Idempotency [CP §3.4]:** upsert on `(account, coin)` and `(account)`; cursor update and writes in
the same transaction. WS reconnects will re-deliver. One extra step the current-state design
needs: when an account closes a position the row must be **deleted**, not left stale — diff the
incoming coin set against what's stored and delete the difference inside the same transaction.
An upsert-only collector would leave closed positions in the map forever, which is the same class
of bug as ChainPulse's unhandled deep reorg **[CP §3.4]** but with a much shorter fuse.

**Discovery** writes `accounts` from the `trades` WS. Seed with the leaderboard and large vaults
so day one isn't cold. The `watchlist`-as-control-plane idea **[CP §1.6]** carries over directly:
the collector reads its coin list and tier thresholds from Postgres each cycle, so the Streamlit
app can promote an account to T0 and see it go live in seconds. That is PerpDesk's version of
ChainPulse's 90-second demo loop.

---

## 5. Schema (phase 1) — current state, normalized

Losing CDF removes the "schema change ⇒ full re-snapshot" penalty, which changes one decision:
**normalize rather than storing positions as `jsonb`.** The `jsonb` plan existed to keep the table
schema stable under the re-snapshot rule. That rule is gone, and `jsonb` over a JDBC federation
link arrives as a string needing `from_json` with a hand-maintained schema in silver — a real cost
for a benefit that no longer exists. Typed columns federate cleanly.

```
positions_current   (account, coin, szi, entry_px, leverage, leverage_type, margin_mode,
                     position_value, margin_used, unrealized_pnl, liquidation_px_reported,
                     observed_at)                              PK (account, coin)
accounts_current    (account, total_raw_usd, account_value_reported, cross_mm_reported,
                     book_hash, observed_at, tier, last_notional)   PK (account)
asset_ctx_current   (coin, mark_px, oracle_px, funding, open_interest, premium, observed_at)
                                                               PK (coin)
meta_margin_tables  (table_id, tier, lower_bound, max_leverage, fetched_at)
accounts_discovered (address, first_seen, last_traded)         -- discovery firehose target
alerts              (id bigserial, raised_at, rule, subject, detail jsonb, acknowledged_by/at)
poll_cursor         (tier, last_address, updated_at)
```

Every table is one-row-per-entity and upserted. Nothing here grows with time except
`accounts_discovered`, which grows with distinct addresses seen and needs a retention policy
(drop anything not traded in 30 days, or cap it and let tier scoring decide).

**Expose a curated view to the foreign catalog rather than the raw tables [CP-rev §5].**
ChainPulse's `transfers_lakehouse` view existed to hide a `numeric(78,0)` column Spark can't read.
PerpDesk has no such column — everything fits `numeric(38,18)` — but the pattern is still right:
a view is a stable contract, so you can reshape the underlying tables without breaking the
pipeline. Define `positions_lakehouse`, `accounts_lakehouse`, `asset_ctx_lakehouse`.

`text` lowercase `0x…` addresses, for ChainPulse's reasons **[CP §1.5]**: debuggable, federates
unchanged, and Genie can index string column values so a pasted address actually matches.

`REPLICA IDENTITY FULL` is **no longer needed** — it exists for logical replication, which is what
CDF used. Harmless to set, but don't put it in the write-up as though it's doing work.

Seed `accounts_current` with leaderboard addresses; set real tier thresholds after the first hour
of discovery, ChainPulse's `03_set_cursor.sql` pattern **[CP §3.5]**.

---

## 6. Phase 2 — auth (now unblocked, ChainPulse verbatim)

`sql/02_roles.sql`, substituting the service principal client ID **[CP §2.2]**:

```sql
CREATE EXTENSION IF NOT EXISTS databricks_auth;
SELECT databricks_create_role('<client-id>', 'SERVICE_PRINCIPAL');
GRANT CONNECT ON DATABASE databricks_postgres TO "<client-id>";
GRANT USAGE  ON SCHEMA public                 TO "<client-id>";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "<client-id>";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "<client-id>";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "<client-id>";
```

The sequence grant is load-bearing — `alerts` uses `bigserial` and the failure mode doesn't
mention sequences. `PGUSER` is the client ID UUID, not a username. Autoscaling means
`w.postgres.generate_database_credential(endpoint="projects/.../endpoints/...")`, not
`w.database.*`. `.env` gitignored before real values go in, `.env.example` committed **[CP §2.3]**.

Check: `SELECT current_user, current_database()` returns the UUID, not your email.

---

## 7. The core computation

Phase 6 in the spec is called out as the long pole. It is long because "the shock grid" as written
is a cross join. Here is the shape that isn't.

### 7.1 Precompute per snapshot

Per position `mm_now(a,c)`; per account `MM_now(a) = Σ_c mm_now(a,c)` and
`AV_now(a) = total_raw_usd + Σ_c szi_c * mark_c` (§1 identity, at current marks).

Use **cross positions only** for the cross solvency test. Isolated positions are evaluated
standalone against their own `marginUsed` — `marginSummary.accountValue` includes them and must
not be used here.

### 7.2 Single-asset shocks — closed form, not a grid

For shock coin `X` at multiplier `s`, only `a`'s `X` leg moves:

```
AV(s) = AV_now + szi_X * px_X * (s - 1)
MM(s) = (MM_now - mm_now_X) + mm_X(|szi_X| * px_X * s)
```

Within tier `k`, `f(s) = AV(s) - MM(s)` is linear:

```
f(s) = [AV_now - szi_X*px_X - MM_rest + ded_k] + s * px_X * (szi_X - |szi_X| * mmr_k)
```

Slope is `+px*|szi|*(1 - mmr_k)` for longs, `-px*|szi|*(1 + mmr_k)` for shorts — strictly signed,
so `f` is monotone with exactly one root. Solve per tier segment, keep the root inside that
segment's range (`s ∈ [lb_k, lb_{k+1}) / (|szi_X| * px_X)`).

`s*` is **the exact joint liquidation price of X for account a**, accounting for the margin drag
of the rest of the book. Not a bucket.

- `gold_account_joint_liq_px` becomes a real table; the validation chart against the API's
  per-position `liquidationPx` is a scatter of exact numbers.
- `gold_liquidation_cliffs` becomes exact: sort by `s*`, cumulative-sum `|notional_X|`, bin at
  25 bp, flag increments above `k ×` the rolling median. A real discontinuity, not a grid artifact.
- `gold_liquidation_map` at any level is a range filter on `s*` — the heatmap is
  `WHERE s* >= 0.95`. Marginal cost per level ≈ 0.
- `O(positions)` instead of `O(positions × levels)`.

Build the 5-level brute-force grid first anyway. It's 20 lines, it's the fallback, and it's the
oracle: grid and closed form must agree on which accounts are liquidatable at each level. That
agreement test is worth more than either artifact alone.

### 7.3 Correlated shocks — explode, it's cheap

```
AV(s) = AV_now + Σ_c szi_c * px_c * (s_c - 1)
liquidatable iff AV(s) < Σ_c mm_c(|szi_c| * px_c * s_c)
```

10–20 scenarios. Explode positions × scenarios, `groupBy(account, scenario)`. ~3M rows. Trivial;
don't over-engineer.

**Stretch:** the closed form extends to a one-parameter family `s_c(β) = 1 - β·beta_c` — piecewise
linear in β with kinks at the union of every coin's tier crossings, solvable per account by
scanning the merged breakpoint list. That yields an exact **joint liquidation beta per account**,
the single strongest number in the project. Guard it: a market-neutral book may have no root in
`β ∈ [0, 0.5]` — emit NULL and count those accounts rather than dropping them.

### 7.4 The headline finding

Per account and coin, compare `s*` against the API's `liquidationPx` as a multiplier. The
distribution of that gap — how much earlier accounts actually liquidate once the rest of the book
is accounted for — is the result. One scatter, one summary statistic, unobtainable elsewhere.

### 7.5 Answer "does this need Spark" honestly

5,000 accounts × 3 positions fits in pandas, and an interviewer who trades will say so. The
defensible answer is the time axis: `gold_liquidation_map_history` accumulates a full map per
refresh (§3), so the artifact is `map × refresh × 30 days`, and the paper ledger scores against
that history. The §10 split makes this concrete rather than rhetorical — the instantaneous map
demonstrably *doesn't* need Spark, which is why it's served from Postgres. Say both halves.
Claiming the instantaneous map is big loses the room.

---

## 8. Pipeline mechanics

`from pyspark import pipelines as dp`, not `import dlt` **[CP §6.1]**.

**Bronze is a batch snapshot of federated Postgres [CP-rev §6].** No `readStream`, no
`create_auto_cdc_flow`, no `_pg_lsn` — none of that exists without CDF. The whole ingest side
collapses to:

```python
@dp.materialized_view(comment="Point-in-time snapshot of live Lakebase position state.")
def bronze_positions_snapshot():
    return (spark.read.table("perpdesk_pg.public.positions_lakehouse")
                 .withColumn("captured_at", F.current_timestamp()))
```

Same for `accounts` and `asset_ctx`. Target catalog is `workspace.perpdesk` — Free Edition has no
external storage, so the tidy `perpdesk.bronze` / `perpdesk.silver` catalog split isn't available
and everything lands in one schema with name prefixes **[CP-rev §6]**.

`captured_at` is the only ordering you get. It's a wall clock, so two refreshes in the same second
are indistinguishable — fine at a refresh cadence of minutes, and worth a comment saying you know
it's weaker than an LSN and why you have no LSN.

**The append task.** The pipeline's gold map materialized view is current-state only. History
comes from a second Job task after the pipeline task:

```sql
INSERT INTO workspace.perpdesk.gold_liquidation_map_history
SELECT * FROM workspace.perpdesk.gold_liquidation_map;
```

One statement, and it's what turns the map into the time series the paper ledger scores against.

**Synced-table prerequisites, declared now not retrofitted [CP §6.4].** The gold map table needs
an explicit `schema=` with `NOT NULL` key columns and `CONSTRAINT ... PRIMARY KEY (...) RELY`,
plus `table_properties={"delta.enableChangeDataFeed": "true"}`. Without these the Create Synced
Table dialog won't detect a key in phase 11 and you'll be rebuilding the table.

**The two expectations that carry the project:**

```python
@dp.expect("mm_matches_exchange",
           "abs(mm_computed - mm_reported) / greatest(mm_reported, 1) < 1e-6")
@dp.expect("av_matches_exchange",
           "abs(av_computed - av_reported) / greatest(abs(av_reported), 1) < 1e-3")
```

`expect`, not `expect_or_drop` — a failure is a finding to count, not a row to hide **[CP §6.3]**.
The AV tolerance is looser than MM because it's exposed to mark drift between the snapshot and the
`asset_ctx` row; measure the real distribution in phase 8 and tighten to what you observe.

---

## 9. Genie

**Five tables or fewer, gold only [CP §7.1]**, all under `workspace.perpdesk` **[CP-rev §7]**:
`gold_liquidation_map`, `gold_liquidation_cliffs`, `gold_account_joint_liq_px`,
`gold_funding_regime`, `gold_paper_ledger`. Nothing from bronze or silver, and **not** the
`perpdesk_pg` foreign catalog — pointing Genie at live Postgres would push interactive query load
onto the database serving the app's hot path.

Column comments are where the framing traps live, because that's where Genie reads them
**[CP §7.2]**. `liquidatable_notional` is a lower bound over tracked accounts; the map is
as-of-last-observed positions at live prices; ledger returns are gross with a stated fee and
slippage assumption.

ChainPulse's `net_position` lesson **[CP §6.4]** transfers exactly and is the highest-stakes
naming decision here: **do not name anything `liquidation_volume` or `expected_liquidations`.**
Call it `liquidatable_notional_tracked`. A name that overclaims will have Genie confidently
reporting a market total to the first knowledgeable person who asks, and the demo dies there.

**Benchmark suite, 15–20 questions with hand-verified answers [CP §7.5]** — the most
differentiating artifact in the project, and the same argument applies here. Include at least two
the agent should *decline*: anything about untracked accounts, and anything asking what *will*
happen rather than what is liquidatable now. A Genie agent that refuses correctly demos better
than one that always answers. Record failures in `genie/benchmarks.yaml` rather than fixing and
forgetting.

The v3 spec's "`n` instruction from v2" is still missing, but ChainPulse §7.5 covers the
substance; treat the paper ledger's hit rate as never reportable without its `n`.

---

## 10. The app, and what "live" now means

Pipeline refresh is manual or scheduled **[CP-rev §5]**, so the map in Delta is minutes old at
best. Calling the Streamlit panel a "live map" while it reads a synced Delta table would be the
same species of overclaim as naming a column `expected_liquidations`.

The honest and better split — and it's ChainPulse's own panel design **[CP §8.2]**:

| Panel | Reads | Freshness | Demonstrates |
|---|---|---|---|
| Live map | **Lakebase directly** — `positions_current` × `asset_ctx_current`, `s*` computed in the query | seconds | OLTP hot path, no lakehouse in the request |
| Map history / cliffs | synced Delta gold table | last refresh | lakehouse-computed, Postgres-served |
| Coverage | synced gold | last refresh | the model reporting its own incompleteness |
| Watchlist / tier editor | writes Postgres | instant | app state in Postgres; closes the loop to the collector |
| Alerts | reads + writes `alerts` | instant | analytical output and app state in one database |

The live panel is genuinely computable in Postgres: `positions_current` is ~15k rows and the
closed-form `s*` from §7.2 is arithmetic, not iteration. Push it down as SQL over the two current
tables. Spark owns the historical and correlated work; Postgres owns the hot path.

This is a stronger demo than the CDF version would have been, because both halves are now doing
work you can point at. Timestamp every panel with its own as-of and let the difference show.

App resource wiring, service principal, and `app.yaml` follow ChainPulse **[CP §8.2]** unchanged —
adding Lakebase as an App resource injects the connection details, so no connection strings in
code. Reuse the same `OAuthConnection` pool.

---

## 11. Repo layout

ChainPulse conventions, since the repo is the deliverable **[CP intro]**.

```
perpdesk/
├── sql/          01_schema.sql  02_roles.sql  03_seed.sql  04_comments.sql  05_metric_views.sql
├── perpdesk/
│   ├── margin.py      PURE: tier resolution (incl. missing-id fallback), mmr/deduction, mm()
│   ├── shocks.py      PURE: AV/MM under shock, closed-form solve
│   └── collector/     db.py [CP §3.1 verbatim]  discovery.py  state_ws.py  state_rest.py
│                      bucket.py  writer.py  main.py
├── pipelines/    bronze.py silver.py gold_map.py gold_cliffs.py gold_funding.py gold_ledger.py
├── genie/        instructions.md  benchmarks.yaml
├── app/          app.py  app.yaml
├── tests/        fixtures/  test_margin.py  test_shocks.py
├── .env  .env.example  README.md
```

`margin.py` and `shocks.py` import no Spark and operate on dicts and floats. They're unit-tested
against the fixtures already captured in `tests/fixtures/`, then wrapped as pandas UDFs. This is
the highest-leverage structural decision in the repo: the math the whole project rests on is
testable in 200 ms without a cluster.

---

## 12. Build order

~13.5 h. Phase 0 is mostly pre-paid, the long pole is removed, and losing CDF made two phases
cheaper than they were.

| Phase | Time | What | Δ |
|---|---|---|---|
| 0 | 30 m | Egress test against HL; Lakebase project PG17 **Autoscaling**; WS/REST cap measurements | API facts pre-verified |
| 1 | 45 m | Schema: current-state tables, normalized, plus the `*_lakehouse` views | −15 m; no re-snapshot risk to design around |
| 2 | 30 m | Service principal + PG role — CP §2 verbatim | now unblocked |
| 3 | 1 h | `margin.py` + `shocks.py` + golden tests against captured fixtures | **new, front-loaded** |
| 4 | 2.5 h | Collector: discovery WS, T0 state WS, tiered REST poller, token bucket, book-hash change detection, **closed-position deletes** | — |
| 5 | 20 m | Register `perpdesk_pg` foreign catalog; verify the views read from Spark | **−10 m**, and far less can go wrong than starting a CDF feed |
| 6 | 1 h | Silver: margin tiers **with missing-id fallback**, both reconciliation expectations | — |
| 7 | 1.5 h | Gold: coarse grid → closed-form `s*` → agreement test between them | **−1 h** |
| 8 | 1.25 h | Gold: cliffs from exact `s*`, correlated scenarios, funding regime, **map-history append task** | +15 m |
| 9 | 1.5 h | Column comments, traps, metric views, Genie, benchmark suite | — |
| 10 | 2 h | Synced gold table + Streamlit, **including the Postgres-side live map query** | +30 m |
| 11 | 1 h | Paper ledger | — |
| 12 | 20 m | **Branching demo** — CP §9 | — |
| 13 | 1.5 h | README + video | — |

Phase 5 is the one that got materially safer. Starting a CDF feed had preconditions (non-empty
tables, replica identity, preview enabled, supported destination) and, per the ChainPulse
revision, fails outright on Free Edition. Registering a foreign catalog is a connection and a
grant. **Do phase 5 immediately after phase 1** — before the collector exists — so you find out
whether federation reads your views correctly while the schema is still cheap to change. The
ChainPulse revision discovered its blocker at phase 5, four phases of work in.

Phase 3 moves before the collector deliberately. The margin math is the load-bearing claim, it's
verified against live data right now, and locking it behind tests means the collector and pipeline
are built on something known-good — and that a total collector failure still leaves a
demonstrable artifact.

**Phase 12 is 20 minutes and should not be cut.** ChainPulse's argument is right and the
distinctive-per-minute ratio is unbeatable: branch the Lakebase project, corrupt the branch, show
production untouched, delete the branch.

**Cut lines in order:** correlated-β closed form → funding regime → paper ledger → alerting.
Never cut the coverage column or the two reconciliation expectations; they're what make it a risk
model instead of a chart.

---

## 13. Write-up angle

ChainPulse's framing **[CP §10]** — a claim you tested, not a tour you gave — but the claim is
different, and PerpDesk has the stronger one:

> *"Every liquidation heatmap on the market is an inference from aggregate open interest. On
> Hyperliquid the per-account state is public and the liquidation rule is published, so the map
> can be computed exactly. I built it, and I proved my implementation matches the exchange's own
> numbers continuously to 1e-6."*

**The claim survives losing CDF, and that's the point worth making explicitly.** ChainPulse's
thesis was about the seam between OLTP and OLAP, so an unavailable seam cost it its headline.
PerpDesk's thesis is about the correctness of a computation over public state. The transport
between Postgres and Delta changed from streaming to scheduled federation, the architecture got
*simpler*, and not one sentence of the finding changed. Say that — a portfolio piece whose central
claim is robust to a platform limitation is making a different and better argument than one whose
claim was the platform.

Report both halves. **Worked:** Unity Catalog federation gave governed access to live Postgres
with no pipeline written; the synced table served lakehouse-computed results back out of Postgres;
Unity Catalog governed the Postgres identity, so there is no database password anywhere in the
system. **Rough:** Lakebase CDF could not be started at all on Free Edition — Databricks-managed
Default Storage is an unsupported destination — so Postgres→Delta became scheduled federation and
the ~15 s replication claim is gone; refresh is minutes, not seconds; Free Edition egress pushed
the collector out of the platform; scale-to-zero cold starts show in the app; coverage is a
partial lower bound by construction; the map has no liquidity model and no behavioural response.

Naming those is the difference between a risk model and a chart — and it's verbatim the
conversation you'd have with a customer in week two, which is the actual point of the exercise.

---

## 14. Still open

- **The "`n` instruction from v2"** — still not in the workspace. CP §7.5 covers the substance
  (never report a hit rate without its `n`), but if the original wording matters, it's needed for
  phase 9.
- **Isolated positions in the map are underspecified.** They liquidate independently, so a
  correlated shock does nothing extra to an isolated book. Report them as a separate series;
  blending hides the distinction that makes the cross-margin finding interesting.
- **Leaderboard seed source.** The spec says "seed with the leaderboard and the large vaults" but
  there's no documented public leaderboard endpoint. Either scrape it once by hand into
  `sql/03_seed.sql` or drop the seed and let discovery cold-start — decide in phase 1.
- **Refresh cadence, and therefore history granularity.** The map history is only as fine-grained
  as the Job schedule, and Free Edition's minimum scheduled-job interval isn't something I could
  confirm. Measure a full pipeline run in phase 8 and set the schedule to something you can
  actually sustain — a 15-minute map that always completes beats a 1-minute one that overlaps
  itself. Record the achieved cadence; it's the denominator on every claim about the history.
- **`accounts_discovered` retention.** It's the one Postgres table that grows without bound, off
  the `trades` firehose. Needs a policy before it's the largest thing in the database — 30-day
  last-traded cutoff is the obvious default, decide in phase 4.
