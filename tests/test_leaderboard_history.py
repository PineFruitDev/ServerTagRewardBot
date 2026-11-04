"""Empty current rankings must not erase members' past participation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.leaderboard import LeaderboardCommand
from src.services.guild_config import GuildConfig
from src.services.streaks import StreakStore, rank


@pytest.mark.parametrize("sort", ["current", "longest", "total"])
async def test_finished_streak_is_only_absent_from_current_ranking(monkeypatch, sort):
    rows = [{"user_id": "42", "active_since": None,
             "longest_seconds": 120, "total_seconds": 180}]
    monkeypatch.setattr(GuildConfig, "_instance",
                        SimpleNamespace(get_role=AsyncMock(return_value="10")))
    monkeypatch.setattr(StreakStore, "_instance", SimpleNamespace(
        leaderboard=AsyncMock(return_value=rank(rows, sort, 10, 1000))))
    interaction = SimpleNamespace(
        guild_id=1, response=SimpleNamespace(send_message=AsyncMock()))

    await LeaderboardCommand()._show(interaction, sort)

    sent = interaction.response.send_message.await_args
    if sort == "current":
        assert "No current streaks" in sent.args[0]
        assert "Nobody has repped" not in sent.args[0]
    else:
        assert "<@42>" in sent.kwargs["embed"].description
