"""调度事件与结算窗口的契约。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class CaseState(StrEnum):
    OPEN = "open"
    APPEALED = "appealed"
    REVIEWED = "reviewed"
    SETTLED = "settled"


@dataclass(frozen=True)
class DispatchInterval:
    instruction_id: str
    plant_id: str
    starts_at: datetime
    ends_at: datetime
    requested_mw: Decimal
