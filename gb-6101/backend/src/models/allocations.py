from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
)

from .base import metadata

# 物理牵引车的全局行锁入口。不同交接批次的 JSON 中出现同一 asset 时，
# 都会同步到这张表，从而让并发领车在同一组车号上串行决策。
vehicle_assets = Table(
    "vehicle_assets",
    metadata,
    Column("asset", String(64), primary_key=True),
    Column("category", String(64), nullable=False),
    Column("created_at", BigInteger, nullable=False),
)

allocation_claims = Table(
    "allocation_claims",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("case_id", ForeignKey("cases.id"), nullable=True, index=True),
    Column("run_id", ForeignKey("runs.id"), nullable=True, index=True),
    Column("actor", String(48), nullable=False),
    Column("team_name", String(120), nullable=False),
    Column("service_day", String(20), nullable=False, index=True),
    Column("start_minute", Integer, nullable=False),
    Column("end_minute", Integer, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("idempotency_key", String(96), nullable=True, unique=True),
    Column("created_at", BigInteger, nullable=False),
    Column("cancelled_at", BigInteger, nullable=True),
    CheckConstraint("start_minute >= 0", name="ck_allocation_start_nonnegative"),
    CheckConstraint("end_minute <= 1440", name="ck_allocation_end_in_day"),
    CheckConstraint("start_minute < end_minute", name="ck_allocation_window_positive"),
    CheckConstraint("quantity > 0", name="ck_allocation_quantity_positive"),
)

allocation_items = Table(
    "allocation_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("claim_id", ForeignKey("allocation_claims.id"), nullable=False, index=True),
    Column("asset", String(64), ForeignKey("vehicle_assets.asset"), nullable=False),
    # 冗余班组、日期和区间，使全局时段冲突查询不必回连主单。
    Column("team_name", String(120), nullable=False),
    Column("service_day", String(20), nullable=False, index=True),
    Column("start_minute", Integer, nullable=False),
    Column("end_minute", Integer, nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("created_at", BigInteger, nullable=False),
    Column("cancelled_at", BigInteger, nullable=True),
    Index("ix_allocation_items_asset_window", "asset", "service_day", "status", "start_minute", "end_minute"),
)
