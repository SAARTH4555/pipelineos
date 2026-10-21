"""Load a portal spec from YAML and turn pydantic errors into one readable message per problem."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from pipelineos.spec.models import PortalSpec


class SpecError(Exception):
    """The spec file is missing, not YAML, or fails validation."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


def load_spec(path: Path) -> PortalSpec:
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise SpecError([f"spec not found: {path}"]) from exc
    except yaml.YAMLError as exc:
        raise SpecError([f"{path} is not valid YAML: {exc}"]) from exc
    try:
        return PortalSpec.model_validate(raw)
    except ValidationError as exc:
        raise SpecError(_problems(exc)) from exc


def _problems(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"])
        message = str(err["msg"]).removeprefix("Value error, ")
        # Cross-object checks join several problems with "; " so the user sees all of them.
        for part in message.split("; "):
            problems.append(f"{where}: {part}" if where else part)
    return problems
