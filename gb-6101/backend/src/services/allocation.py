"""单数模块名兼容入口；实现位于 ``services.allocations``。"""

from .allocations import *  # noqa: F401,F403
from .allocations import cancel_claim, cancel_item, create, serialize_claim  # noqa: F401


def allocate(db, data, actor):
    return create(db, data, actor)


def cancel(db, allocation_id, actor="system"):
    return cancel_claim(db, allocation_id, actor)
