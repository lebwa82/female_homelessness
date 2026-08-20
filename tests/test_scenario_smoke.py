import pytest

from scripts.scenario_smoke import run_scenarios


@pytest.mark.asyncio
async def test_scenario_smoke_covers_aid_request_and_critical_escalation() -> None:
    await run_scenarios()
