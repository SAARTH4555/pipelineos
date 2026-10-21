"""Turn a classification (plus company size from the CRM) into a segment, a queue and a reason.

The model only reads the text. Who a lead goes to is decided here, by rules a RevOps lead can
read and change, and each decision carries a one-line reason so a rep can see why it landed on
them. Company size from the CRM wins over a size typed into the form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipelineos.ai.classify import Classification

Segment = Literal["self_serve", "mid_market", "enterprise"]
Queue = Literal["ae_enterprise", "sdr_mid_market", "self_serve_nurture"]

ENTERPRISE_SIZE = 500
MID_MARKET_SIZE = 50
QUEUE_FOR: dict[Segment, Queue] = {
    "enterprise": "ae_enterprise",
    "mid_market": "sdr_mid_market",
    "self_serve": "self_serve_nurture",
}


@dataclass(frozen=True)
class Routing:
    segment: Segment
    queue: Queue
    reason: str


def route(c: Classification, company_employees: int | None = None) -> Routing:
    size = company_employees if company_employees is not None else c.team_size
    size_note = (
        f"{size:,} people ({'CRM' if company_employees is not None else 'form'})"
        if size is not None
        else "size unknown"
    )
    asks = "asked for enterprise requirements" if c.enterprise_signals else "no enterprise asks"
    segment: Segment
    if (size is not None and size >= ENTERPRISE_SIZE) or (
        c.enterprise_signals and (size is None or size >= MID_MARKET_SIZE)
    ):
        segment = "enterprise"
    elif (size is not None and size >= MID_MARKET_SIZE) or c.enterprise_signals:
        segment = "mid_market"
    else:
        segment = "self_serve"
    if c.use_case == "other" and not c.enterprise_signals and segment != "enterprise":
        segment = "self_serve"
        reason = f"No testing problem described; {size_note}"
    else:
        reason = f"{c.use_case.replace('_', ' ')}; {size_note}; {asks}"
    return Routing(segment, QUEUE_FOR[segment], reason)
