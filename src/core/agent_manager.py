import os
from utils.logger import logger
from agents.agent_a import AgentA

class AgentManager:
    """Manages the lifecycle and lazy loading of AI Agents."""

    def __init__(self, mode: str = "auto"):
        self.mode = mode
        self.agent_a = AgentA(mode=mode)
        self.agent_b = None  # LoRA (Lazy load)
        self.agent_c = None  # Knowledge (Lazy load)
        self.agent_d = None  # Finalist (Lazy load)
        
        # Numerical Quant Agents (Lazy load)
        self.scout = None
        self.quant_agent = None
        self.validator = None
        self.curator = None

        logger.info(f"AgentManager initialized in {mode} mode")

    def lazy_load_agents(self, need_b=False, need_c=False, need_d=False, need_quant=False):
        """Helper to load AI agents only when needed."""
        if need_c and self.agent_c is None:
            from agents.agent_c import AgentC
            self.agent_c = AgentC()
            logger.info("AgentC (Knowledge) lazy loaded")
            
        if need_b and self.agent_b is None:
            from agents.agent_b import AgentB
            self.agent_b = AgentB()
            logger.info("AgentB (LoRA) lazy loaded")
            
        if need_d and self.agent_d is None:
            from agents.agent_d import AgentD
            self.agent_d = AgentD()
            logger.info("AgentD (Finalist) lazy loaded")

        if need_quant:
            if self.scout is None:
                from agents.quant.scout import ScoutAgent
                self.scout = ScoutAgent()
            if self.quant_agent is None:
                from agents.quant.quant_agent import QuantAgent
                self.quant_agent = QuantAgent()
            if self.validator is None:
                from agents.quant.validator import ValidatorAgent
                self.validator = ValidatorAgent()
            if self.curator is None:
                from agents.quant.curator import CuratorAgent
                self.curator = CuratorAgent()
            logger.info("Numerical Quant Agents lazy loaded")

    def clear_agents(self):
        """Deep clean: Remove all agent instances and release GPU memory."""
        logger.info("Deep cleaning AI agents and releasing resources...")

        if self.agent_b:
            del self.agent_b
            self.agent_b = None
        if self.agent_c:
            self.agent_c.stop_background_sync()
            del self.agent_c
            self.agent_c = None
        if self.agent_d:
            del self.agent_d
            self.agent_d = None
            
        self.scout = None
        self.quant_agent = None
        self.validator = None
        self.curator = None

        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        logger.info("All GPU resources released.")
