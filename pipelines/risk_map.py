"""PerpDesk Lakeflow Declarative Pipeline.

Source tables are batch snapshots of a Unity Catalog foreign catalog because
Lakebase CDF cannot target Free Edition's Databricks-managed default storage.
Postgres stays bounded current state; Delta owns the time axis.
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window

from perpdesk.margin import Tier
from perpdesk.shocks import AccountPosition, joint_liquidation_multiplier


SOURCE = spark.conf.get("perpdesk.source_catalog", "perpdesk_pg.public")


@dp.materialized_view(comment="Point-in-time snapshot of live Lakebase account state.")
def bronze_accounts_snapshot():
    return spark.read.table(f"{SOURCE}.accounts_lakehouse").withColumn(
        "captured_at", F.current_timestamp()
    )


@dp.materialized_view(comment="Point-in-time snapshot of current tracked positions.")
def bronze_positions_snapshot():
    return spark.read.table(f"{SOURCE}.positions_lakehouse").withColumn(
        "captured_at", F.current_timestamp()
    )


@dp.materialized_view(comment="Point-in-time snapshot of live public asset marks and open interest.")
def bronze_asset_ctx_snapshot():
    return spark.read.table(f"{SOURCE}.asset_ctx_lakehouse").withColumn(
        "captured_at", F.current_timestamp()
    )


@dp.materialized_view(comment="Complete margin tiers, including synthesised bare-id schedules.")
def bronze_margin_tiers_snapshot():
    return spark.read.table(f"{SOURCE}.margin_tiers_lakehouse").withColumn(
        "captured_at", F.current_timestamp()
    )


@dp.materialized_view(comment="Cross positions marked consistently at the current public mark.")
def silver_positions_marked():
    positions = spark.read.table("bronze_positions_snapshot").filter(
        F.col("leverage_type") == "cross"
    )
    marks = spark.read.table("bronze_asset_ctx_snapshot").select(
        "coin", "mark_px", "open_interest_notional", "funding"
    )
    tiers = spark.read.table("bronze_margin_tiers_snapshot").drop("captured_at").withColumn(
        "mmr", F.lit(1.0) / (F.lit(2.0) * F.col("max_leverage"))
    )
    # ded(k) = sum(lb_i * (mmr_i - mmr_{i-1})) through the applicable tier.
    tier_window = Window.partitionBy("margin_table_id").orderBy("tier")
    tiers = tiers.withColumn("previous_mmr", F.lag("mmr").over(tier_window))
    tiers = tiers.withColumn(
        "tier_deduction_piece",
        F.when(F.col("previous_mmr").isNull(), F.lit(0.0)).otherwise(
            F.col("lower_bound") * (F.col("mmr") - F.col("previous_mmr"))
        ),
    ).withColumn(
        "deduction",
        F.sum("tier_deduction_piece").over(
            tier_window.rowsBetween(Window.unboundedPreceding, Window.currentRow)
        ),
    )
    candidates = (
        positions.join(marks, "coin")
        .join(tiers, "margin_table_id")
        .withColumn("notional_now", F.abs(F.col("szi") * F.col("mark_px")))
        .filter(F.col("lower_bound") <= F.col("notional_now"))
    )
    applicable = Window.partitionBy("account", "coin").orderBy(F.col("tier").desc())
    return (
        candidates.withColumn("rank", F.row_number().over(applicable))
        .filter(F.col("rank") == 1)
        .drop("rank", "previous_mmr", "tier_deduction_piece")
        .withColumn("mm_computed", F.col("notional_now") * F.col("mmr") - F.col("deduction"))
    )


@dp.expect(
    "mm_matches_exchange",
    "abs(mm_computed - mm_reported) / greatest(mm_reported, 1) < 1e-6",
)
@dp.expect(
    "av_matches_exchange",
    "abs(av_computed - av_reported) / greatest(abs(av_reported), 1) < 1e-3",
)
@dp.materialized_view(
    comment="Continuous reconciliation against exchange-reported account value and cross maintenance margin."
)
def silver_account_reconciliation():
    positions = spark.read.table("silver_positions_marked")
    totals = positions.groupBy("account").agg(
        F.sum(F.col("szi") * F.col("mark_px")).alias("signed_position_value"),
        F.sum("mm_computed").alias("mm_computed"),
    )
    return (
        spark.read.table("bronze_accounts_snapshot")
        .join(totals, "account", "left")
        .fillna({"signed_position_value": 0.0, "mm_computed": 0.0})
        .withColumn("av_computed", F.col("total_raw_usd") + F.col("signed_position_value"))
        .withColumnRenamed("account_value_reported", "av_reported")
        .withColumnRenamed("cross_mm_reported", "mm_reported")
    )


ROOT_SCHEMA = T.ArrayType(
    T.StructType(
        [
            T.StructField("coin", T.StringType(), False),
            T.StructField("direction", T.StringType(), False),
            T.StructField("joint_liq_multiplier", T.DoubleType()),
            T.StructField("joint_liq_px", T.DoubleType()),
            T.StructField("marginal_liq_multiplier", T.DoubleType()),
            T.StructField("notional_now", T.DoubleType(), False),
        ]
    )
)


@F.udf(ROOT_SCHEMA)
def solve_account_roots(total_raw_usd, raw_positions):
    positions = []
    for raw in raw_positions or []:
        tiers = tuple(
            Tier(float(t.lower_bound), int(t.max_leverage), float(t.mmr), float(t.deduction))
            for t in raw.tiers
        )
        positions.append(
            AccountPosition(
                raw.coin,
                float(raw.szi),
                float(raw.mark_px),
                tiers,
                "cross",
                float(raw.liquidation_px_reported) if raw.liquidation_px_reported else None,
            )
        )
    result = []
    for position in positions:
        root = joint_liquidation_multiplier(float(total_raw_usd), positions, position.coin)
        result.append(
            {
                "coin": position.coin,
                "direction": "down" if position.szi > 0 else "up",
                "joint_liq_multiplier": root,
                "joint_liq_px": root * position.mark_px if root is not None else None,
                "marginal_liq_multiplier": (
                    position.liquidation_px_reported / position.mark_px
                    if position.liquidation_px_reported is not None
                    else None
                ),
                "notional_now": position.notional,
            }
        )
    return result


@dp.materialized_view(
    comment=(
        "Exact joint liquidation price per tracked cross account and coin; other book margin is included. "
        "joint_vs_marginal_gap_pct is the percentage-point price move by which joint liquidation is closer "
        "to the current mark than marginal liquidation; positive means the per-position view understates "
        "risk. It is null when either liquidation multiplier is unavailable."
    )
)
def gold_account_joint_liq_px():
    positions = spark.read.table("silver_positions_marked")
    tier_rows = spark.read.table("bronze_margin_tiers_snapshot").drop("captured_at").withColumn(
        "mmr", F.lit(1.0) / (F.lit(2.0) * F.col("max_leverage"))
    )
    tier_window = Window.partitionBy("margin_table_id").orderBy("tier")
    tier_rows = (
        tier_rows.withColumn("previous_mmr", F.lag("mmr").over(tier_window))
        .withColumn(
            "piece",
            F.when(F.col("previous_mmr").isNull(), F.lit(0.0)).otherwise(
                F.col("lower_bound") * (F.col("mmr") - F.col("previous_mmr"))
            ),
        )
        .withColumn(
            "deduction",
            F.sum("piece").over(tier_window.rowsBetween(Window.unboundedPreceding, Window.currentRow)),
        )
        .groupBy("margin_table_id")
        .agg(F.sort_array(F.collect_list(F.struct("lower_bound", "max_leverage", "mmr", "deduction"))).alias("tiers"))
    )
    books = (
        positions.join(tier_rows, "margin_table_id")
        .groupBy("account")
        .agg(
            F.collect_list(
                F.struct("coin", "szi", "mark_px", "liquidation_px_reported", "tiers")
            ).alias("positions")
        )
        .join(spark.read.table("bronze_accounts_snapshot").select("account", "total_raw_usd", "captured_at"), "account")
        .withColumn("root", F.explode(solve_account_roots("total_raw_usd", "positions")))
    )
    return books.select("account", "captured_at", "root.*").withColumn(
        "joint_vs_marginal_gap_pct",
        (
            F.abs(F.col("marginal_liq_multiplier") - 1.0)
            - F.abs(F.col("joint_liq_multiplier") - 1.0)
        )
        * 100,
    )


SHOCK_LEVELS = (1, 2, 3, 5, 7, 10, 15, 20)


@dp.materialized_view(
    comment="Tracked liquidatable notional by exact shock threshold. Values are lower bounds, never market totals."
)
def gold_liquidation_map():
    roots = (
        spark.read.table("gold_account_joint_liq_px")
        .filter(F.col("joint_liq_multiplier").isNotNull())
        .withColumnRenamed("direction", "root_direction")
    )
    contexts = spark.read.table("bronze_asset_ctx_snapshot").select("coin", "open_interest_notional")
    shocks = spark.createDataFrame(
        [("down", value, 1.0 - value / 100.0) for value in SHOCK_LEVELS]
        + [("up", value, 1.0 + value / 100.0) for value in SHOCK_LEVELS],
        "scenario_direction string, shock_pct int, multiplier double",
    )
    crossed = (
        roots.crossJoin(shocks)
        .filter(F.col("root_direction") == F.col("scenario_direction"))
        .filter(
            ((F.col("root_direction") == "down") & (F.col("multiplier") <= F.col("joint_liq_multiplier")))
            | ((F.col("root_direction") == "up") & (F.col("multiplier") >= F.col("joint_liq_multiplier")))
        )
    )
    result = crossed.groupBy("coin", F.col("root_direction").alias("direction"), "shock_pct").agg(
        F.sum("notional_now").alias("liquidatable_notional_tracked"),
        F.countDistinct("account").alias("accounts_liquidatable_tracked"),
        F.max("captured_at").alias("captured_at"),
    )
    return (
        result.join(contexts, "coin", "left")
        .withColumn(
            "coverage_fraction_open_interest",
            F.when(F.col("open_interest_notional") > 0, F.col("liquidatable_notional_tracked") / F.col("open_interest_notional")),
        )
        .withColumn("map_key", F.concat_ws(":", "coin", "direction", F.col("shock_pct").cast("string")))
    )


@dp.materialized_view(comment="25bp exact joint-liquidation cliffs over tracked notional.")
def gold_liquidation_cliffs():
    roots = spark.read.table("gold_account_joint_liq_px").filter(F.col("joint_liq_multiplier").isNotNull())
    binned = roots.withColumn("multiplier_bin", F.round(F.col("joint_liq_multiplier") * 400) / 400)
    grouped = binned.groupBy("coin", "direction", "multiplier_bin").agg(
        F.sum("notional_now").alias("cliff_notional_tracked"),
        F.countDistinct("account").alias("accounts_at_cliff"),
        F.max("captured_at").alias("captured_at"),
    )
    rolling = Window.partitionBy("coin", "direction").orderBy("multiplier_bin").rowsBetween(-10, -1)
    return grouped.withColumn("rolling_median_notional", F.percentile_approx("cliff_notional_tracked", 0.5).over(rolling)).withColumn(
        "is_cliff", F.col("cliff_notional_tracked") > 3 * F.col("rolling_median_notional")
    )


@dp.materialized_view(comment="Current funding regime for contextualising forced-deleveraging risk.")
def gold_funding_regime():
    return spark.read.table("bronze_asset_ctx_snapshot").select(
        "coin",
        "funding",
        F.when(F.col("funding") > 0.0001, "longs_crowded")
        .when(F.col("funding") < -0.0001, "shorts_crowded")
        .otherwise("neutral")
        .alias("regime"),
        "captured_at",
    )
