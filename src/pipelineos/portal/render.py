"""Plain-text rendering of plans, apply results and the manual checklist.

Output is deliberately plain (no colour codes) so it reads the same in a terminal, a CI log and
the README, where it is pasted verbatim.
"""

from __future__ import annotations

from pipelineos.portal.apply import ApplyResult
from pipelineos.portal.plan import Op, Plan
from pipelineos.spec.models import PortalSpec

SYMBOL = {Op.CREATE: "+", Op.UPDATE: "~", Op.MANUAL: "!", Op.UNMANAGED: "?"}


def render_plan(plan: Plan, *, verbose: bool = False) -> str:
    shown = [a for a in plan.actions if verbose or a.op is not Op.NOOP]
    lines: list[str] = []
    width = max((len(a.target) for a in shown), default=0)
    for action in shown:
        symbol = SYMBOL.get(action.op, " ")
        detail = action.detail
        if action.op is Op.UNMANAGED:
            detail = f"{detail} (left alone)"
        lines.append(f"  {symbol} {action.kind:<10} {action.target:<{width}}  {detail}".rstrip())
    counts = plan.counts()
    lines += [
        "",
        f"Plan: {counts[Op.CREATE]} to create, {counts[Op.UPDATE]} to update, "
        f"{counts[Op.MANUAL]} need manual action, {counts[Op.UNMANAGED]} unmanaged "
        f"(left alone), {counts[Op.NOOP]} unchanged.",
    ]
    return "\n".join(lines)


def render_checklist(spec: PortalSpec) -> str:
    lines = ["Manual checklist (HubSpot UI only):"]
    for object_name, pipelines in spec.pipelines.items():
        lines.append(
            f"  [ ] Pipelines: set the properties required to enter each {object_name} stage"
        )
        for pipeline in pipelines:
            for stage in pipeline.stages:
                if stage.required_properties:
                    props = ", ".join(stage.required_properties)
                    lines.append(f"        {pipeline.label} / {stage.label}: {props}")
    for step in spec.manual:
        if step.area == "Pipelines":
            continue  # covered by the generated per-stage list above
        lines.append(f"  [ ] {step.area}: {step.step}")
    return "\n".join(lines)


def render_apply(result: ApplyResult, total: int) -> str:
    if result.ok:
        return f"Applied {len(result.applied)} of {total} changes."
    assert result.failed is not None
    return (
        f"Applied {len(result.applied)} of {total} changes, then failed on "
        f"{result.failed.kind} {result.failed.target}: {result.error}\n"
        "Fix the cause and run apply again; it continues from where it stopped."
    )
