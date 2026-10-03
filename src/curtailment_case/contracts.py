"""调度事件与结算窗口的契约。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class CaseState(StrEnum):
    """争议案件状态机：OPEN -> APPEALED -> REVIEWED -> SETTLED。

    允许的转移：
    - OPEN -> APPEALED          场站提交申诉
    - APPEALED -> REVIEWED      复核得出结论
    - REVIEWED -> REVIEWED      复核结论变更（产生新的复核版本）
    - REVIEWED -> SETTLED       纳入已确认的结算版本
    - SETTLED -> REVIEWED       结算后复核结论变更（不改写已确认版本，需新版本结算）
    """

    OPEN = "open"
    APPEALED = "appealed"
    REVIEWED = "reviewed"
    SETTLED = "settled"


class Attribution(StrEnum):
    """限发归因类别。"""

    EQUIPMENT_FAULT = "equipment_fault"  # 设备故障
    DISPATCH_INSTRUCTION = "dispatch_instruction"  # 指令限发
    NETWORK_CONSTRAINT = "network_constraint"  # 网络约束
    UNATTRIBUTED = "unattributed"  # 未归因


class ReviewOutcome(StrEnum):
    """复核结论。"""

    UPHELD = "upheld"  # 维持原归因与电量
    REJECTED = "rejected"  # 驳回申诉
    PARTIAL = "partial"  # 部分支持（须给出调整后电量）


class InstructionEventType(StrEnum):
    """调度指令事件类型：补发/下发、更正、撤销。"""

    ISSUE = "issue"
    CORRECT = "correct"
    REVOKE = "revoke"


@dataclass(frozen=True)
class DispatchInterval:
    """某一知识时点下可见的有效调度指令区间。"""

    instruction_id: str
    plant_id: str
    starts_at: datetime
    ends_at: datetime
    requested_mw: Decimal
