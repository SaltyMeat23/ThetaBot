"""AI weekly summary generator: prose on success, fail-open (None) otherwise."""
import pytest

from agentic.ai.weekly import generate_weekly_summary

WEEK = {"realized_pnl": 40, "wins": 2, "losses": 0, "win_rate": 1.0,
        "credit_collected_resolved": 50, "by_rule": []}
CUM = {"realized_pnl": 260, "resolved_count": 12, "win_rate": 0.83}
ROWS = [{"status": "OPEN", "underlying": "SOFI", "strike": 17.0, "dte": 7, "unrealized_pnl": -20}]


class FakeSummaryClient:
    def __init__(self, text="Solid week.", raises=False):
        self._text, self._raises = text, raises
        self.saw_user = None

    async def summarize(self, system, user):
        if self._raises:
            raise RuntimeError("boom")
        self.saw_user = user
        return self._text


@pytest.mark.asyncio
async def test_summary_ok_and_passes_data():
    c = FakeSummaryClient("Steady week; watch SOFI.")
    out = await generate_weekly_summary(c, week_stats=WEEK, cumulative=CUM, rows=ROWS)
    assert out == "Steady week; watch SOFI."
    assert "SOFI" in c.saw_user and "realized_pnl" in c.saw_user   # the week's data reached the model


@pytest.mark.asyncio
async def test_summary_none_when_no_client():
    assert await generate_weekly_summary(None, week_stats=WEEK, cumulative=CUM, rows=ROWS) is None


@pytest.mark.asyncio
async def test_summary_fails_open_on_error():
    c = FakeSummaryClient(raises=True)
    assert await generate_weekly_summary(c, week_stats=WEEK, cumulative=CUM, rows=ROWS) is None
