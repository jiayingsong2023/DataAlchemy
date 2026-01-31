import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.agents.coordinator import Coordinator

@pytest.fixture
def mock_coordinator():
    with patch('src.agents.agent_a.AgentA'), \
         patch('src.agents.agent_b.AgentB'), \
         patch('src.agents.agent_c.AgentC'), \
         patch('src.agents.agent_d.AgentD'), \
         patch('src.utils.s3_utils.S3Utils'), \
         patch('src.core.agent_manager.AgentA'): # Also patch where it is used in AgentManager
        coord = Coordinator(mode="python")
        yield coord

def test_coordinator_init(mock_coordinator):
    """Test that the coordinator initializes correctly with mocked agents."""
    assert mock_coordinator.mode == "python"
    assert mock_coordinator.agent_manager.agent_a is not None

@pytest.mark.asyncio
async def test_coordinator_chat_smoke(mock_coordinator):
    """Smoke test for the chat functionality."""
    # Mocking lazy loaded agents
    mock_coordinator.agent_manager.agent_b = MagicMock()
    mock_coordinator.agent_manager.agent_c = MagicMock()
    mock_coordinator.agent_manager.agent_d = MagicMock()
    
    mock_coordinator.agent_manager.agent_c.query.return_value = "mock_context"
    mock_coordinator.agent_manager.agent_b.predict_async = AsyncMock(return_value="mock_intuition")
    mock_coordinator.agent_manager.agent_d.fuse_and_respond.return_value = "mock_answer"
    
    # Mocking session and feedback
    mock_coordinator.save_feedback = MagicMock(return_value="fake_id")
    
    response = await mock_coordinator.chat_async("Hello")
    assert response == "mock_answer"

def test_ingestion_pipeline_smoke(mock_coordinator):
    """Smoke test for the ingestion pipeline trigger."""
    mock_coordinator.agent_manager.agent_a.run_cleaning = MagicMock()
    
    # Mock the instance method already attached to coordinator
    mock_coordinator.pipeline_manager.run_ingestion_pipeline = MagicMock()
    
    mock_coordinator.run_ingestion_pipeline(stage="wash")
    mock_coordinator.pipeline_manager.run_ingestion_pipeline.assert_called_once_with("wash", False, None)
