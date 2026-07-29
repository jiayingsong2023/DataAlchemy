from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.core.pipeline import PipelineManager


def test_ingestion_stops_when_cleaning_fails():
    agents = SimpleNamespace(
        agent_a=MagicMock(clean_and_split=MagicMock(return_value={"status": "error", "reason": "boom"})),
        lazy_load_agents=MagicMock(),
    )
    pipeline = PipelineManager(agents, MagicMock())

    with pytest.raises(RuntimeError, match="Data cleaning failed"):
        pipeline.run_ingestion_pipeline()


def test_ingestion_propagates_synthesis_failure():
    agents = SimpleNamespace(
        agent_a=MagicMock(clean_and_split=MagicMock(return_value={"status": "success"})),
        lazy_load_agents=MagicMock(),
    )
    pipeline = PipelineManager(agents, MagicMock())
    pipeline._handle_synthesis = MagicMock(side_effect=RuntimeError("synthesis failed"))

    with pytest.raises(RuntimeError, match="synthesis failed"):
        pipeline.run_ingestion_pipeline(synthesis=True)
