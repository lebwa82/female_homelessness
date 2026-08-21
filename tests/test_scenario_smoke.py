import pytest

from scripts.scenario_smoke import main, run_scenarios


@pytest.mark.asyncio
async def test_scenario_smoke_covers_aid_request_and_critical_escalation() -> None:
    await run_scenarios()


def test_scenario_smoke_reports_all_acceptance_paths(capsys: pytest.CaptureFixture[str]) -> None:
    main()

    assert capsys.readouterr().out == (
        "Scenario smoke: aid, open conversation, psychologist request and crisis paths passed\n"
    )
