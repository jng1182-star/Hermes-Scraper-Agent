"""
SoS History Database — SQLite time-series store for Share of Spend / Share of Voice snapshots.

Purpose:
  SoS is only meaningful as a trend. A single snapshot tells you today's share;
  a time-series reveals whether a competitor is ramping or pulling back spend.
  This module writes one row per brand per run, enabling trend queries across sessions.

Schema:
  sos_snapshots — one row per brand per pipeline run
  run_logs      — audit trail of pipeline executions

Usage:
  from data.sos_db import SosDB
  SosDB().save_snapshot(report_dict, market="Philippines", industry="beauty")
"""

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(__file__), "sos_history.db")


def _get_engine():
    try:
        from sqlalchemy import create_engine
        return create_engine(f"sqlite:///{_DB_PATH}", future=True)
    except ImportError:
        return None


def _ensure_tables(engine) -> None:
    from sqlalchemy import (
        Column, DateTime, Float, Index, Integer, MetaData, String, Table, inspect, text
    )
    meta = MetaData()

    sos_table = Table(
        "sos_snapshots", meta,
        Column("id",                   Integer, primary_key=True, autoincrement=True),
        Column("run_id",               String,  nullable=False, index=True),
        Column("run_date",             DateTime, nullable=False),
        Column("brand",                String,  nullable=False),
        Column("platform",             String,  nullable=False),
        Column("market",               String,  nullable=False, default=""),
        Column("industry",             String,  nullable=False, default=""),
        Column("post_type",            String,  nullable=False, default="both"),
        Column("paid_signal",          String,  nullable=True),
        Column("confidence_tier",      String,  nullable=True),
        Column("sos_pct",              Float,   nullable=True),
        Column("sov_pct",              Float,   nullable=True),
        Column("estimated_spend_usd",  Float,   nullable=True),
        Column("inferred_impressions", Integer, nullable=True),
        Column("impression_method",    String,  nullable=True),
        Column("engagement_rate_pct",  Float,   nullable=True),
        Column("benchmark_er_pct",     Float,   nullable=True),
        Column("er_vs_benchmark",      Float,   nullable=True),
        Column("cpm_used_usd",         Float,   nullable=True),
        Column("vtr_used",             Float,   nullable=True),
    )
    # W3: explicit composite and single-column indexes for the common query patterns
    Index("ix_sos_market",    sos_table.c.market)
    Index("ix_sos_industry",  sos_table.c.industry)
    Index("ix_sos_run_date",  sos_table.c.run_date)
    Index("ix_sos_market_industry_date", sos_table.c.market, sos_table.c.industry, sos_table.c.run_date)

    Table(
        "run_logs", meta,
        Column("id",        Integer, primary_key=True, autoincrement=True),
        Column("run_id",    String,  nullable=False),
        Column("timestamp", DateTime, nullable=False),
        Column("status",    String,  nullable=False),  # SUCCESS | WARNING | ERROR
        Column("message",   String,  nullable=True),
    )

    # create_all is a no-op on already-existing tables, so run it first to
    # create new tables if the DB is fresh.
    meta.create_all(engine)

    # W4: lightweight migration guard — add any columns that are missing on
    # pre-existing tables (ALTER TABLE ADD COLUMN is safe and idempotent on SQLite).
    insp = inspect(engine)
    existing_cols = {c["name"] for c in insp.get_columns("sos_snapshots")}
    new_cols = {col.name: col for col in sos_table.columns if col.name not in ("id",)}
    with engine.begin() as conn:
        for col_name, col_obj in new_cols.items():
            if col_name not in existing_cols:
                col_type = col_obj.type.compile(engine.dialect)
                try:
                    conn.execute(text(
                        f"ALTER TABLE sos_snapshots ADD COLUMN {col_name} {col_type}"
                    ))
                    logger.info("[SosDB] Migration: added column '%s' to sos_snapshots.", col_name)
                except Exception as exc:
                    logger.warning("[SosDB] Migration: could not add column '%s': %s", col_name, exc)


class SosDB:
    def __init__(self):
        self._engine = _get_engine()
        if self._engine is not None:
            try:
                _ensure_tables(self._engine)
            except Exception as exc:
                logger.warning("[SosDB] Table init failed: %s", exc)
                self._engine = None

    def save_snapshot(self, report: dict, market: str = "", industry: str = "") -> bool:
        """
        Persist all brand-level SoS/SoV rows from a completed report.
        Returns True on success, False on failure (non-blocking — caller should not crash).
        """
        if self._engine is None:
            logger.warning("[SosDB] SQLAlchemy not available — skipping snapshot.")
            return False

        try:
            from sqlalchemy import text
            run_id   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_date = datetime.now(timezone.utc)
            competitors = report.get("competitors", [])

            # S6: build a brand→vtr_used lookup from the assumptions block
            breakdowns = report.get("assumptions", {}).get("brand_breakdowns", [])
            vtr_lookup: dict[str, float | None] = {
                bd.get("brand", ""): bd.get("vtr_used")
                for bd in breakdowns
                if bd.get("impression_method") == "video_vtr"
            }

            rows = []
            for comp in competitors:
                brand_name = comp.get("name", "Unknown")
                rows.append({
                    "run_id":               run_id,
                    "run_date":             run_date,
                    "brand":                brand_name,
                    "platform":             comp.get("platform", ""),
                    "market":               market,
                    "industry":             industry,
                    "post_type":            comp.get("post_type", "both"),
                    "paid_signal":          comp.get("paid_signal"),
                    "confidence_tier":      comp.get("confidence_tier"),
                    "sos_pct":              comp.get("sos_pct"),
                    "sov_pct":              comp.get("sov_pct"),
                    "estimated_spend_usd":  comp.get("estimated_spend_usd"),
                    "inferred_impressions": comp.get("inferred_impressions"),
                    "impression_method":    comp.get("impression_method"),
                    "engagement_rate_pct":  comp.get("engagement_rate"),
                    "benchmark_er_pct":     comp.get("benchmark_er_pct"),
                    "er_vs_benchmark":      comp.get("er_vs_benchmark"),
                    "cpm_used_usd":         comp.get("cpm_used"),
                    "vtr_used":             vtr_lookup.get(brand_name),
                })

            if not rows:
                return False

            insert_snapshot = text("""
                INSERT INTO sos_snapshots (
                    run_id, run_date, brand, platform, market, industry,
                    post_type, paid_signal, confidence_tier,
                    sos_pct, sov_pct, estimated_spend_usd, inferred_impressions,
                    impression_method, engagement_rate_pct, benchmark_er_pct,
                    er_vs_benchmark, cpm_used_usd, vtr_used
                ) VALUES (
                    :run_id, :run_date, :brand, :platform, :market, :industry,
                    :post_type, :paid_signal, :confidence_tier,
                    :sos_pct, :sov_pct, :estimated_spend_usd, :inferred_impressions,
                    :impression_method, :engagement_rate_pct, :benchmark_er_pct,
                    :er_vs_benchmark, :cpm_used_usd, :vtr_used
                )
            """)

            insert_log = text("""
                INSERT INTO run_logs (run_id, timestamp, status, message)
                VALUES (:run_id, :timestamp, :status, :message)
            """)

            with self._engine.begin() as conn:
                for row in rows:
                    conn.execute(insert_snapshot, row)
                conn.execute(insert_log, {
                    "run_id":    run_id,
                    "timestamp": run_date,
                    "status":    "SUCCESS",
                    "message":   f"{len(rows)} brand snapshot(s) saved. Market: {market or 'Global'}.",
                })

            logger.info("[SosDB] Saved %d brand snapshot(s) for run %s.", len(rows), run_id)
            return True

        except Exception as exc:
            logger.error("[SosDB] save_snapshot failed: %s", exc)
            try:
                from sqlalchemy import text as _text
                with self._engine.begin() as conn:
                    conn.execute(_text(
                        "INSERT INTO run_logs (run_id, timestamp, status, message) "
                        "VALUES (:run_id, :timestamp, :status, :message)"
                    ), {
                        "run_id":    "error",
                        "timestamp": datetime.now(timezone.utc),
                        "status":    "ERROR",
                        "message":   str(exc),
                    })
            except Exception:
                pass
            return False

    def get_history(self, market: str = "", industry: str = "",
                    limit: int = 200) -> list[dict]:
        """
        Return the most recent SoS snapshots, optionally filtered by market/industry.
        Used by the GET /history API endpoint.
        """
        if self._engine is None:
            return []
        try:
            from sqlalchemy import text
            filters = []
            params: dict = {"limit": limit}
            if market:
                filters.append("market = :market")
                params["market"] = market
            if industry:
                filters.append("industry = :industry")
                params["industry"] = industry
            where = "WHERE " + " AND ".join(filters) if filters else ""
            q = text(f"""
                SELECT * FROM sos_snapshots {where}
                ORDER BY run_date DESC LIMIT :limit
            """)
            with self._engine.connect() as conn:
                rows = conn.execute(q, params).mappings().all()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("[SosDB] get_history failed: %s", exc)
            return []
