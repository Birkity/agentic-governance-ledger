"""Interim concurrency test placeholder.

This file exists now to match the interim deliverable path. The concrete
double-decision concurrency test will be finalized during Phase 1 once the
real EventStore append semantics are implemented against the flattened src/
layout.
"""

import pytest


pytestmark = pytest.mark.skip(reason="Finalize in Phase 1 after EventStore implementation")
