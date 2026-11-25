from time import time
from typing import Any

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.domain.allocation import (
    ACTIVE,
    CANCELLED,
    AllocationRuleError,
    choose_assets,
    eligible_assets,
    normalize_asset_list,
    normalize_day,
    normalize_window,
    time_text,
)
from src.models.allocations import allocation_claims, allocation_items, vehicle_assets
from src.models.cases import cases
from src.models.runs import runs
from .audit import record


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _dialect_name(db) -> str:
    # 路由传入 Connection；部分测试/调用方可能传入 ORM Session。
    dialect = getattr(db, "dialect", None)
    if dialect is None:
        dialect = getattr(getattr(db, "bind", None), "dialect", None)
    return getattr(dialect, "name", "") or ""


def normalize_command(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise HTTPException(422, "领车请求须为 JSON 对象")

    start = _first(data, "start_minute", "startMinute", "start", "startTime", "start_time",
                   "start_at", "startAt", "from_minute", "from_time", "from", "begin")
    end = _first(data, "end_minute", "endMinute", "end", "endTime", "end_time",
                 "end_at", "endAt", "to_minute", "to_time", "to", "finish")
    try:
        start_minute, end_minute = normalize_window(start, end)
        quantity = _first(data, "quantity", "vehicle_count", "vehicleCount", "count",
                          "needed", "needed_count", "neededCount", "number", "num", "qty")
        quantity = 1 if quantity is None else quantity
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 400:
            raise AllocationRuleError("领车数量须为 1–400 的整数")
        preferred = normalize_asset_list(_first(data, "asset_ids", "assets", "vehicle_ids", "preferred_assets"))
        qualified = normalize_asset_list(_first(
            data, "qualified_assets", "eligible_assets", "available_assets",
            "free_assets", "qualified_vehicle_ids", "eligible_vehicle_ids",
        ))
        vehicles_raw = data.get("vehicles")
        if not qualified and isinstance(vehicles_raw, list) and vehicles_raw and all(isinstance(item, str) for item in vehicles_raw):
            qualified = normalize_asset_list(vehicles_raw)
        service_day = normalize_day(_first(data, "service_day", "serviceDate", "date", "day"))
    except AllocationRuleError:
        raise
    except (TypeError, ValueError) as exc:
        raise AllocationRuleError("领车时间或数量格式不正确") from exc

    case_id = _first(data, "case_id", "caseId", "batch_id", "batchId",
                     "handover_batch_id", "handoverBatchId", "shift_id", "shiftId")
    run_id = _first(data, "run_id", "runId", "execution_id")
    try:
        case_id = int(case_id) if case_id is not None and not isinstance(case_id, bool) else None
        run_id = int(run_id) if run_id is not None and not isinstance(run_id, bool) else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "批次编号不正确") from exc

    team_name = str(_first(data, "team_name", "teamName", "team_id", "teamId", "team", "group_name") or "").strip()[:120]
    idempotency_key = _first(data, "idempotency_key", "idempotencyKey", "idempotency-key",
                             "request_id", "requestId", "request_token", "client_token")
    if idempotency_key is not None:
        idempotency_key = str(idempotency_key).strip()[:96] or None

    payload = _first(data, "payload", "business_payload")
    if payload is None and isinstance(data.get("vehicles"), list) and data["vehicles"] and isinstance(data["vehicles"][0], dict):
        payload = data
    if payload is not None and not isinstance(payload, dict):
        raise HTTPException(422, "payload 须为业务资料对象")

    # 只给指定车号而未给批次/检证时，将这些车号视为调用方确认的合格候选。
    qualified_assets = qualified
    if not qualified_assets and preferred and case_id is None and run_id is None and payload is None:
        qualified_assets = preferred

    return {
        "case_id": case_id,
        "run_id": run_id,
        "payload": payload,
        "team_name": team_name,
        "service_day": service_day,
        "start_minute": start_minute,
        "end_minute": end_minute,
        "quantity": quantity,
        "preferred": preferred,
        "qualified": qualified_assets,
        "idempotency_key": idempotency_key,
        "note": str(data.get("note") or "")[:420],
    }


def _load_payload(db, command: dict[str, Any]) -> dict[str, Any]:
    payload = command["payload"]
    case_id = command["case_id"]
    run_id = command["run_id"]

    if run_id is not None:
        row = db.execute(select(runs.c.case_id).where(runs.c.id == run_id)).first()
        if row is None:
            raise HTTPException(404, "执行批次不存在")
        case_id = row[0]
        command["case_id"] = case_id

    if payload is not None:
        return payload
    if case_id is None:
        raise HTTPException(422, "请选择交接批次，或在请求中提供车辆检证资料")

    row = db.execute(select(cases.c.payload).where(cases.c.id == case_id)).first()
    if row is None:
        raise HTTPException(404, "交接批次资料不存在")
    return row[0]


def _sync_vehicle_rows(db, payload: dict[str, Any], now: int) -> list[str]:
    vehicles = payload.get("vehicles") if isinstance(payload, dict) else None
    if not isinstance(vehicles, list):
        raise HTTPException(422, "业务资料缺少 vehicles 列表")

    values: list[dict[str, Any]] = []
    assets: list[str] = []
    for row in vehicles:
        if not isinstance(row, dict) or not isinstance(row.get("asset"), str) or not row["asset"].strip():
            raise HTTPException(422, "vehicles 中存在无法识别的车号")
        asset = row["asset"].strip()[:64]
        category = str(row.get("category") or "未分类")[:64]
        if asset not in assets:
            assets.append(asset)
            values.append({"asset": asset, "category": category, "created_at": now})

    if not values:
        raise HTTPException(422, "交接批次中没有牵引车")

    ordered_values = sorted(values, key=lambda row: row["asset"])
    dialect = _dialect_name(db)
    if dialect == "postgresql":
        stmt = pg_insert(vehicle_assets).values(ordered_values).on_conflict_do_nothing(index_elements=["asset"])
    elif dialect == "sqlite":
        stmt = sqlite_insert(vehicle_assets).values(ordered_values).on_conflict_do_nothing(index_elements=["asset"])
    elif dialect == "mysql":
        stmt = mysql_insert(vehicle_assets).values(ordered_values).on_duplicate_key_update(asset=vehicle_assets.c.asset)
    else:
        stmt = insert(vehicle_assets).values(ordered_values)
    # 固定插入/加锁顺序，降低跨批次资料中车号顺序不同导致的死锁概率。
    db.execute(stmt)
    return assets


def _lock_assets(db, assets: list[str]) -> None:
    statement = (
        select(vehicle_assets.c.asset)
        .where(vehicle_assets.c.asset.in_(assets))
        .order_by(vehicle_assets.c.asset)
    )
    if _dialect_name(db) in {"postgresql", "mysql"}:
        statement = statement.with_for_update()
    db.execute(statement).all()


def _overlapping_assets(db, command: dict[str, Any], assets: list[str]) -> set[str]:
    if not assets:
        return set()
    rows = db.execute(
        select(allocation_items.c.asset).where(
            allocation_items.c.asset.in_(assets),
            allocation_items.c.service_day == command["service_day"],
            allocation_items.c.status == ACTIVE,
            allocation_items.c.start_minute < command["end_minute"],
            allocation_items.c.end_minute > command["start_minute"],
        )
    ).scalars()
    return set(rows)


def _item_dict(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["start"] = data.get("start_minute")
    data["end"] = data.get("end_minute")
    data["start_time"] = time_text(data.get("start_minute"))
    data["end_time"] = time_text(data.get("end_minute"))
    data["vehicle_id"] = data.get("asset")
    data["allocation_id"] = data.get("id")
    data["cancel_url"] = f"/api/allocations/items/{data['id']}/cancel"
    data["canceled_at"] = data.get("cancelled_at")
    data["canceled"] = data.get("status") == CANCELLED
    data["cancelled"] = data.get("status") == CANCELLED
    return data


def serialize_claim(db, claim_id: int) -> dict[str, Any]:
    claim = db.execute(
        select(allocation_claims).where(allocation_claims.c.id == claim_id)
    ).mappings().first()
    if claim is None:
        raise HTTPException(404, "领用单不存在")
    data = dict(claim)
    items = db.execute(
        select(allocation_items)
        .where(allocation_items.c.claim_id == claim_id)
        .order_by(allocation_items.c.id)
    ).mappings().all()
    item_data = [_item_dict(dict(row)) for row in items]
    assets = [item["asset"] for item in item_data]
    data.update({
        "success": True,
        "ok": True,
        "start": data["start_minute"],
        "end": data["end_minute"],
        "start_time": time_text(data["start_minute"]),
        "end_time": time_text(data["end_minute"]),
        "allocation_id": data["id"],
        "cancel_url": f"/api/allocations/{data['id']}/cancel",
        "batch_id": data.get("case_id"),
        "team": data.get("team_name"),
        "date": data.get("service_day"),
        "canceled_at": data.get("cancelled_at"),
        "canceled": data.get("status") == CANCELLED,
        "cancelled": data.get("status") == CANCELLED,
        "assets": assets,
        "asset_ids": assets,
        "vehicle_ids": assets,
        "tractor_ids": assets,
        "car_ids": assets,
        "allocated_assets": assets,
        "allocated_asset_ids": assets,
        "allocated_vehicles": item_data,
        "reserved_vehicles": item_data,
        "released_assets": assets if data.get("status") == CANCELLED else [],
        "vehicles": item_data,
        "items": item_data,
        "allocations": item_data,
    })
    return data


def find_idempotent(db, key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    claim_id = db.scalar(select(allocation_claims.c.id).where(allocation_claims.c.idempotency_key == key))
    return serialize_claim(db, claim_id) if claim_id is not None else None


def create(db, data: dict[str, Any], actor: dict[str, Any] | str) -> dict[str, Any]:
    username = actor["username"] if isinstance(actor, dict) else str(actor)
    command = normalize_command(data)

    existing = find_idempotent(db, command["idempotency_key"])
    if existing is not None:
        return existing

    now = int(time())
    if command["qualified"]:
        # 调用方已经给出“整段时段合格”的车号集合；仍同步全局车号表以便行锁。
        if command["case_id"] is not None or command["run_id"] is not None or command["payload"] is not None:
            _load_payload(db, command)
        eligible = list(dict.fromkeys(command["qualified"]))
        candidate_assets = _sync_vehicle_rows(db, {
            "vehicles": [{"asset": asset, "category": "已核验车辆"} for asset in eligible]
        }, now)
    else:
        payload = _load_payload(db, command)
        candidate_assets = _sync_vehicle_rows(db, payload, now)
        try:
            eligible = eligible_assets(payload, command["start_minute"], command["end_minute"])
        except (TypeError, KeyError) as exc:
            raise HTTPException(422, "检证资料格式不正确，无法判断整段时段有效性") from exc

    if command["preferred"]:
        unknown = [asset for asset in command["preferred"] if asset not in candidate_assets]
        if unknown:
            raise HTTPException(422, f"指定车辆不在可分配批次中：{', '.join(unknown)}")

    # 锁住本批次候选车辆，而不仅是最终中选车号；并发事务会在同一车号上
    # 串行，随后能读到前一事务已提交的领用时段。
    _lock_assets(db, candidate_assets)
    occupied = _overlapping_assets(db, command, eligible)

    # 不足时抛出 ValueError 子类；整个事务尚未写入领用主/明细，回滚后不占车。
    chosen = choose_assets(eligible, occupied, command["quantity"], command["preferred"])

    team_name = command["team_name"] or username
    claim_change = db.execute(insert(allocation_claims).values(
        case_id=command["case_id"],
        run_id=command["run_id"],
        actor=username,
        team_name=team_name,
        service_day=command["service_day"],
        start_minute=command["start_minute"],
        end_minute=command["end_minute"],
        quantity=command["quantity"],
        status=ACTIVE,
        idempotency_key=command["idempotency_key"],
        created_at=now,
    ))
    claim_id = claim_change.inserted_primary_key[0]
    db.execute(insert(allocation_items), [
        {
            "claim_id": claim_id,
            "asset": asset,
            "team_name": team_name,
            "service_day": command["service_day"],
            "start_minute": command["start_minute"],
            "end_minute": command["end_minute"],
            "status": ACTIVE,
            "created_at": now,
        }
        for asset in chosen
    ])
    record(db, "allocate", username, "allocation", claim_id)
    return serialize_claim(db, claim_id)


def list_claims(db, *, case_id: int | None = None, run_id: int | None = None,
                service_day: str | None = None, status_filter: str | None = None,
                limit: int = 100) -> list[dict[str, Any]]:
    statement = select(allocation_claims.c.id).order_by(allocation_claims.c.id.desc()).limit(min(limit, 200))
    if case_id is not None:
        statement = statement.where(allocation_claims.c.case_id == case_id)
    if run_id is not None:
        statement = statement.where(allocation_claims.c.run_id == run_id)
    if service_day is not None:
        statement = statement.where(allocation_claims.c.service_day == service_day)
    if status_filter is not None:
        statement = statement.where(allocation_claims.c.status == status_filter)
    ids = list(db.execute(statement).scalars())
    return [serialize_claim(db, claim_id) for claim_id in ids]


def cancel_claim(db, claim_id: int, actor: dict[str, Any] | str) -> dict[str, Any]:
    username = actor["username"] if isinstance(actor, dict) else str(actor)
    claim = db.execute(
        select(allocation_claims).where(allocation_claims.c.id == claim_id).with_for_update()
        if _dialect_name(db) in {"postgresql", "mysql"}
        else select(allocation_claims).where(allocation_claims.c.id == claim_id)
    ).mappings().first()
    if claim is None:
        # 允许 /allocations/{id} 同时接收明细 ID。
        return cancel_item(db, claim_id, actor)

    if claim["status"] != CANCELLED:
        now = int(time())
        db.execute(
            allocation_claims.update()
            .where(allocation_claims.c.id == claim_id, allocation_claims.c.status == ACTIVE)
            .values(status=CANCELLED, cancelled_at=now)
        )
        db.execute(
            allocation_items.update()
            .where(allocation_items.c.claim_id == claim_id, allocation_items.c.status == ACTIVE)
            .values(status=CANCELLED, cancelled_at=now)
        )
        record(db, "cancel_allocation", username, "allocation", claim_id)
    return serialize_claim(db, claim_id)


def cancel_item(db, item_id: int, actor: dict[str, Any] | str) -> dict[str, Any]:
    username = actor["username"] if isinstance(actor, dict) else str(actor)
    statement = select(allocation_items).where(allocation_items.c.id == item_id)
    if _dialect_name(db) in {"postgresql", "mysql"}:
        statement = statement.with_for_update()
    item = db.execute(statement).mappings().first()
    if item is None:
        raise HTTPException(404, "领用记录不存在")

    if item["status"] != CANCELLED:
        now = int(time())
        db.execute(
            allocation_items.update()
            .where(allocation_items.c.id == item_id, allocation_items.c.status == ACTIVE)
            .values(status=CANCELLED, cancelled_at=now)
        )
        # 若整单所有明细均已取消，同步主单状态，方便列表和幂等重试。
        active_count = db.scalar(
            select(allocation_items.c.id)
            .where(allocation_items.c.claim_id == item["claim_id"], allocation_items.c.status == ACTIVE)
            .limit(1)
        )
        if active_count is None:
            db.execute(
                allocation_claims.update()
                .where(allocation_claims.c.id == item["claim_id"], allocation_claims.c.status == ACTIVE)
                .values(status=CANCELLED, cancelled_at=now)
            )
        record(db, "cancel_vehicle", username, "allocation_item", item_id)
    refreshed = db.execute(select(allocation_items).where(allocation_items.c.id == item_id)).mappings().first()
    return _item_dict(dict(refreshed))


# 常见动作命名别名。
allocate = create
create_allocation = create
release_allocation = cancel_claim
cancel_allocation = cancel_claim
release_item = cancel_item
cancel_reservation = cancel_item
