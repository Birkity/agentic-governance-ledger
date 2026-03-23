"""Counterfactual projection helpers."""

from src.what_if.projector import WhatIfOutcome, WhatIfResult, collect_related_events, replay_projection_snapshot, run_what_if

__all__ = [
    "WhatIfOutcome",
    "WhatIfResult",
    "collect_related_events",
    "replay_projection_snapshot",
    "run_what_if",
]
