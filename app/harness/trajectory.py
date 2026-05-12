"""JSONL trajectory recording for deterministic harness runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TrajectoryRecorder:
    """Append scenario-scoped events to a JSONL trajectory file."""

    path: Path
    scenario_id: str
    _sequence: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        row = {
            "scenario_id": self.scenario_id,
            "sequence": self._sequence,
            "event": event,
            "data": data or {},
        }
        self._sequence += 1
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return row


def reset_trajectory_file(path: Path) -> None:
    """Create an empty trajectory file, replacing any previous benchmark run."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
