"""牵引车时段领用的纯业务规则。

时间使用当日整数分钟，区间统一按半开区间 ``[start, end)`` 处理：
因此 09:00 归还与 09:00 领车不重叠。必检有效性则必须覆盖区间内每个
占用分钟，而不是只看提交时的某一个复核时点。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

ACTIVE = "active"
CANCELLED = "cancelled"


class AllocationRuleError(ValueError):
    """输入或候选车辆不满足领用规则。"""


class InsufficientVehicles(AllocationRuleError):
    def __init__(self, message: str, *, required: int, eligible: int, free: int):
        super().__init__(message)
        self.required = required
        self.eligible = eligible
        self.free = free


def as_minute(value: Any, field_name: str = "时间") -> int:
    if isinstance(value, bool):
        raise AllocationRuleError(f"{field_name}须为当日分钟或 HH:MM 时间")
    if isinstance(value, int):
        minute = value
    elif isinstance(value, float) and value.is_integer():
        minute = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise AllocationRuleError(f"{field_name}不能为空")
        if text.isdigit():
            minute = int(text)
        else:
            # 兼容 ISO 时间、“YYYY-MM-DD HH:MM”以及 HH:MM[:SS]。
            if "T" in text:
                text = text.split("T", 1)[1]
            elif " " in text:
                text = text.split()[-1]
            parts = text.split(":", 2)
            if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise AllocationRuleError(f"{field_name}须为 HH:MM 或当日整数分钟")
            hour, minute_text = int(parts[0]), int(parts[1])
            if hour == 24 and minute_text == 0 and (len(parts) < 3 or int(parts[2]) == 0):
                minute = 1440
            else:
                if not 0 <= hour <= 23 or not 0 <= minute_text <= 59:
                    raise AllocationRuleError(f"{field_name}须为合法的当日时间")
                minute = hour * 60 + minute_text
    else:
        raise AllocationRuleError(f"{field_name}须为当日分钟或 HH:MM 时间")

    if not 0 <= minute <= 1440:
        raise AllocationRuleError(f"{field_name}须在 0–1440 分钟之间")
    return minute


def time_text(minute: int | None) -> str | None:
    if minute is None:
        return None
    return f"{minute // 60:02d}:{minute % 60:02d}"


def normalize_day(value: Any = None) -> str:
    if value is None or value == "":
        return date.today().isoformat()
    if isinstance(value, bool):
        raise AllocationRuleError("服务日期格式不正确")
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if not text:
        return date.today().isoformat()
    # 接受 2026-11-25、2026-11-25T08:00:00 以及更宽松的日期标识。
    return text[:10] if len(text) >= 10 and text[4] == "-" and text[7] == "-" else text


def normalize_window(start: Any = None, end: Any = None, **aliases: Any) -> tuple[int, int]:
    if start is None:
        start = aliases.get("start_minute")
    if start is None:
        start = aliases.get("start_time")
    if end is None:
        end = aliases.get("end_minute")
    if end is None:
        end = aliases.get("end_time")
    start_minute = as_minute(start, "用车开始时间")
    end_minute = as_minute(end, "用车结束时间")
    if start_minute >= end_minute:
        raise AllocationRuleError("用车结束时间必须晚于开始时间；当日时段不跨日")
    return start_minute, end_minute


def intervals_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """半开区间相交判定；``end_a == start_b`` 时返回 False。"""
    return start_a < end_b and start_b < end_a


def _rows(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = payload.get(key, []) if isinstance(payload, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _latest_per_minute(evidence: Iterable[dict[str, Any]], asset: str, check: str) -> list[dict[str, Any]]:
    """同一检测分钟内只有 seq 最大的记录会成为当时的最新证据。"""
    grouped: dict[int, dict[str, Any]] = {}
    for row in evidence:
        if row.get("asset") != asset or row.get("check") != check:
            continue
        checked = row.get("checked")
        seq = row.get("seq", 0)
        if not isinstance(checked, int) or isinstance(checked, bool):
            continue
        old = grouped.get(checked)
        if old is None or (isinstance(seq, int) and seq > old.get("seq", -1)):
            grouped[checked] = row
    return [grouped[key] for key in sorted(grouped)]


def check_valid_through(
    payload: dict[str, Any], asset: str, check: str, start: int, end: int
) -> tuple[bool, str | None]:
    evidence = _rows(payload, "evidence")
    winners = _latest_per_minute(evidence, asset, check)
    current: dict[str, Any] | None = None
    next_index = 0
    for index, row in enumerate(winners):
        if row["checked"] <= start:
            current = row
            next_index = index + 1
        else:
            next_index = index
            break

    if current is None:
        return False, f"{check} 无当时回执"

    while current is not None:
        if next_index < len(winners) and winners[next_index]["checked"] < end:
            boundary = winners[next_index]["checked"]
        else:
            boundary = end

        # boundary 是下一条证据生效或用车结束的分钟；当前最新证据必须
        # 至少有效到该半开区间的右端点。
        expires = current.get("expires")
        if current.get("status") != "pass":
            return False, f"{check} 存在不通过结论"
        if not isinstance(expires, int) or isinstance(expires, bool) or expires < boundary:
            return False, f"{check} 在用车时段内失效"

        if boundary == end:
            return True, None

        current = winners[next_index]
        next_index += 1

    return False, f"{check} 无有效证据"


def evaluate_vehicle(
    payload: dict[str, Any], asset: str, start: int, end: int
) -> dict[str, Any]:
    vehicles = {row.get("asset"): row for row in _rows(payload, "vehicles")}
    vehicle = vehicles.get(asset)
    if vehicle is None:
        return {"asset": asset, "eligible": False, "category": None, "reasons": ["车辆不在交接批次中"]}

    category = vehicle.get("category")
    requirements = sorted({
        row.get("check")
        for row in _rows(payload, "requirements")
        if row.get("category") == category and isinstance(row.get("check"), str)
    })
    if not requirements:
        return {
            "asset": asset,
            "category": category,
            "eligible": False,
            "reasons": ["车型未配置必检项目"],
        }

    reasons: list[str] = []
    for check in requirements:
        valid, reason = check_valid_through(payload, asset, check, start, end)
        if not valid and reason:
            reasons.append(reason)
    return {
        "asset": asset,
        "category": category,
        "eligible": not reasons,
        "reasons": reasons or ["全部必检在整个用车时段有效"],
    }


def eligible_assets(payload: dict[str, Any], start: int, end: int) -> list[str]:
    assets = [row.get("asset") for row in _rows(payload, "vehicles") if isinstance(row.get("asset"), str)]
    # 去重并保持车号顺序稳定；资料校验通常已禁止重复车号。
    ordered = list(dict.fromkeys(assets))
    return [asset for asset in ordered if evaluate_vehicle(payload, asset, start, end)["eligible"]]


def normalize_asset_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items: list[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raise AllocationRuleError("指定车辆须为车号数组")

    assets: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            asset = item.strip()
        elif isinstance(item, dict):
            raw_asset = item.get("asset") or item.get("vehicle_id") or item.get("id")
            asset = raw_asset.strip() if isinstance(raw_asset, str) else ""
        else:
            asset = ""
        if not asset:
            raise AllocationRuleError("指定车辆中存在无法识别的车号")
        if asset not in assets:
            assets.append(asset)
    return assets


def choose_assets(
    eligible: Iterable[str],
    occupied: Iterable[str],
    quantity: int,
    preferred: Iterable[str] | None = None,
) -> list[str]:
    eligible_list = list(dict.fromkeys(eligible))
    eligible_set = set(eligible_list)
    occupied_set = set(occupied)
    preferred_list = list(preferred or [])

    for asset in preferred_list:
        if asset not in eligible_set:
            raise AllocationRuleError(f"车辆 {asset} 不满足整段用车时间的必检要求")

    chosen: list[str] = []
    for asset in preferred_list:
        if asset not in occupied_set and asset not in chosen and len(chosen) < quantity:
            chosen.append(asset)

    for asset in eligible_list:
        if len(chosen) >= quantity:
            break
        if asset not in occupied_set and asset not in chosen:
            chosen.append(asset)

    if len(chosen) < quantity:
        raise InsufficientVehicles(
            f"整段时段合格且空闲的牵引车不足：需要 {quantity} 辆，"
            f"合格 {len(eligible_list)} 辆，当前空闲 {sum(a not in occupied_set for a in eligible_list)} 辆；本次未占用车辆",
            required=quantity,
            eligible=len(eligible_list),
            free=sum(a not in occupied_set for a in eligible_list),
        )
    return chosen


def occupied_from_reservations(
    reservations: Iterable[dict[str, Any]], start: int, end: int, service_day: Any = None
) -> set[str]:
    """从内存中的领用记录提取与给定时段冲突的车号，便于纯领域测试。"""
    day = normalize_day(service_day) if service_day is not None else None
    occupied: set[str] = set()
    for reservation in reservations or []:
        if not isinstance(reservation, dict):
            continue
        if reservation.get("status") == CANCELLED:
            continue
        if day is not None:
            reservation_day = reservation.get("service_day") or reservation.get("date")
            if reservation_day is not None and normalize_day(reservation_day) != day:
                continue
        asset = reservation.get("asset") or reservation.get("vehicle_id")
        raw_start = reservation.get("start_minute", reservation.get("start"))
        raw_end = reservation.get("end_minute", reservation.get("end"))
        if not isinstance(asset, str) or raw_start is None or raw_end is None:
            continue
        reservation_start = as_minute(raw_start, "已有领用开始时间")
        reservation_end = as_minute(raw_end, "已有领用结束时间")
        if intervals_overlap(start, end, reservation_start, reservation_end):
            occupied.add(asset)
    return occupied


def allocate_vehicles(
    payload: dict[str, Any],
    start: Any = None,
    end: Any = None,
    quantity: int = 1,
    occupied: Iterable[str] | None = None,
    reservations: Iterable[dict[str, Any]] | None = None,
    preferred: Iterable[str] | None = None,
    service_day: Any = None,
    **window_aliases: Any,
) -> dict[str, Any]:
    """无数据库依赖的领车决策；数据库服务在锁行后调用同一套规则。"""
    start_minute, end_minute = normalize_window(start, end, **window_aliases)
    eligible = eligible_assets(payload, start_minute, end_minute)
    occupied_set = set(occupied or [])
    occupied_set.update(occupied_from_reservations(
        reservations or [], start_minute, end_minute, service_day
    ))
    assets = choose_assets(eligible, occupied_set, quantity, preferred)
    return {
        "assets": assets,
        "vehicle_ids": assets,
        "quantity": quantity,
        "start_minute": start_minute,
        "end_minute": end_minute,
        "start_time": time_text(start_minute),
        "end_time": time_text(end_minute),
        "service_day": normalize_day(service_day),
        "status": ACTIVE,
    }


# 常见命名别名。
is_overlapping = intervals_overlap
vehicle_eligibility = evaluate_vehicle
select_eligible_assets = eligible_assets


def vehicle_available(payload: dict[str, Any], asset: str, start: int, end: int) -> bool:
    return bool(evaluate_vehicle(payload, asset, start, end)["eligible"])


def is_vehicle_available(payload: dict[str, Any], asset: str, start: int, end: int,
                         reservations: Iterable[dict[str, Any]] | None = None) -> bool:
    if not vehicle_available(payload, asset, start, end):
        return False
    return asset not in occupied_from_reservations(reservations or [], start, end)
