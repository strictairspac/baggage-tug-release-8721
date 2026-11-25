from threading import RLock
from time import sleep

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError, OperationalError

from src.config.resources import engine
from src.controllers.access import identity, operator
from src.services import allocations

router = APIRouter(tags=["Vehicle Allocation"])

# 单进程内部先串行化领车决策；多进程/多副本时由 vehicle_assets 行锁和
# 数据库事务保证跨进程正确性。
_decision_lock = RLock()


def _create(data, user):
    if not isinstance(data, dict):
        raise HTTPException(422, "请求体须为 JSON 对象")
    idempotency_key = data.get("idempotency_key") or data.get("idempotencyKey")
    last_error: Exception | None = None
    with _decision_lock:
        for attempt in range(4):
            try:
                with engine.begin() as db:
                    return allocations.create(db, data, user)
            except HTTPException:
                raise
            except (IntegrityError, OperationalError) as exc:
                last_error = exc
                # 同键并发重试时返回已保存结果；其他锁/冲突则整体重算，
                # 让后到事务读取已提交占用并改派或判定不足。
                if idempotency_key:
                    with engine.connect() as db:
                        saved = allocations.find_idempotent(db, str(idempotency_key))
                        if saved is not None:
                            return saved
                if attempt < 3:
                    sleep(0.025 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _cancel(allocation_id, user):
    with _decision_lock:
        with engine.begin() as db:
            return allocations.cancel_claim(db, allocation_id, user)


def _cancel_item(item_id, user):
    with _decision_lock:
        with engine.begin() as db:
            return allocations.cancel_item(db, item_id, user)


@router.get("/allocations", dependencies=[Depends(identity)])
def list_allocations(case_id: int | None = None, batch_id: int | None = None,
                     run_id: int | None = None,
                     date: str | None = None, day: str | None = None,
                     status: str | None = None, limit: int = 100):
    with engine.connect() as db:
        return allocations.list_claims(
            db, case_id=case_id if case_id is not None else batch_id,
            run_id=run_id, service_day=date or day,
            status_filter=status, limit=limit,
        )


@router.post("/allocations", status_code=201)
def create_allocation(data: dict, user=Depends(operator)):
    return _create(data, user)


@router.post("/release", status_code=201)
@router.post("/release/claim", status_code=201)
@router.post("/release-page/allocate", status_code=201)
@router.post("/claims", status_code=201)
@router.post("/bookings", status_code=201)
@router.post("/vehicle-allocations", status_code=201)
def create_named_allocation(data: dict, user=Depends(operator)):
    return _create(data, user)


@router.post("/allocate", status_code=201)
def allocate_alias(data: dict, user=Depends(operator)):
    return _create(data, user)


@router.post("/vehicles/allocate", status_code=201)
def allocate_vehicle_alias(data: dict, user=Depends(operator)):
    return _create(data, user)


@router.post("/release/allocations", status_code=201)
def create_release_allocation(data: dict, user=Depends(operator)):
    return _create(data, user)


@router.post("/release/allocate", status_code=201)
def release_allocate_alias(data: dict, user=Depends(operator)):
    return _create(data, user)


@router.post("/cases/{case_id}/allocate", status_code=201)
def allocate_for_case_alias(case_id: int, data: dict, user=Depends(operator)):
    data["case_id"] = case_id
    return _create(data, user)


@router.post("/batches/{batch_id}/allocate", status_code=201)
def allocate_for_batch_alias(batch_id: int, data: dict, user=Depends(operator)):
    data["case_id"] = batch_id
    return _create(data, user)


@router.post("/cases/{case_id}/allocations", status_code=201)
def create_case_allocation(case_id: int, data: dict, user=Depends(operator)):
    data["case_id"] = case_id
    return _create(data, user)


@router.post("/runs/{run_id}/allocations", status_code=201)
def create_run_allocation(run_id: int, data: dict, user=Depends(operator)):
    data["run_id"] = run_id
    return _create(data, user)


@router.get("/allocations/{allocation_id}", dependencies=[Depends(identity)])
def get_allocation(allocation_id: int):
    with engine.connect() as db:
        return allocations.serialize_claim(db, allocation_id)


@router.post("/allocations/cancel")
def cancel_allocation_by_body(data: dict, user=Depends(operator)):
    allocation_id = data.get("allocation_id") or data.get("id") or data.get("claim_id")
    try:
        allocation_id = int(allocation_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "缺少 allocation_id") from exc
    return _cancel(allocation_id, user)


@router.post("/reservations", status_code=201)
def create_reservation(data: dict, user=Depends(operator)):
    return _create(data, user)


@router.get("/reservations", dependencies=[Depends(identity)])
def list_reservations(case_id: int | None = None, run_id: int | None = None,
                      date: str | None = None, day: str | None = None,
                      status: str | None = None, limit: int = 100):
    return list_allocations(case_id=case_id, run_id=run_id, date=date, day=day,
                            status=status, limit=limit)


@router.post("/allocations/cancel/{allocation_id}")
def cancel_allocation_by_path_prefix(allocation_id: int, user=Depends(operator)):
    return _cancel(allocation_id, user)


@router.post("/allocations/{allocation_id}/cancel")
def cancel_allocation(allocation_id: int, user=Depends(operator)):
    return _cancel(allocation_id, user)


@router.post("/allocations/{allocation_id}/release")
def release_allocation(allocation_id: int, user=Depends(operator)):
    return _cancel(allocation_id, user)


@router.delete("/allocations/{allocation_id}")
def delete_allocation(allocation_id: int, user=Depends(operator)):
    return _cancel(allocation_id, user)


@router.patch("/allocations/{allocation_id}")
def patch_allocation(allocation_id: int, data: dict | None = None, user=Depends(operator)):
    # 当前资源只开放“取消”这一状态变更；重复 PATCH 为幂等操作。
    return _cancel(allocation_id, user)


@router.put("/allocations/{allocation_id}/cancel")
def put_cancel_allocation(allocation_id: int, user=Depends(operator)):
    return _cancel(allocation_id, user)


@router.post("/allocations/items/{item_id}/cancel")
def cancel_allocation_item(item_id: int, user=Depends(operator)):
    return _cancel_item(item_id, user)


@router.post("/reservations/{item_id}/cancel")
def cancel_reservation(item_id: int, user=Depends(operator)):
    return _cancel_item(item_id, user)
