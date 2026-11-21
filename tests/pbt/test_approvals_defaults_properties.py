"""Property 6 of auto-approval: the default severity of a rule made from a proposal."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from approvals_helpers import make_manager  # noqa: E402

from chimericmesh.diagnostics import Incident  # noqa: E402


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.integers(min_value=0, max_value=7))
def test_property_6_default_severity_is_min4_of_max3_severity(severity):
    rig = make_manager("bare")

    async def go():
        await rig.add_service()
        inc = Incident(id="i1", host="web01", category="disk", signature="disk full", severity=severity, count=3,
                       first_seen=0.0, last_seen=0.0, service_id="api-gw")
        rig.hub.tracker.install(inc)
        prop = await rig.hub.create_proposal("i1", "fix")
        await rig.hub.decide(prop.id, True, "alice")
        return rig.manager.rule_defaults(prop.id).most_urgent_severity

    try:
        got = asyncio.run(go())
    finally:
        rig.close()
    assert got == min(4, max(3, severity)) and got in (3, 4)
