"""
Agent S: The Scheduler.
Responsible for periodic tasks and system auto-evolution.
"""
import time
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Configure logging for the scheduler
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] Agent S: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("AgentS")

class AgentS:
    """Scheduler Agent to automate Data Alchemy and Training."""
    
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.scheduler = BlockingScheduler()
        self._is_running = False
        self.synthesis = False
        self.max_samples = None

    def scheduled_task(self):
        """The task that runs every 24 hours."""
        if self._is_running:
            logger.warning("Previous task is still running. Skipping this cycle.")
            return
            
        self._is_running = True
        start_time = datetime.now()
        logger.info(f"Starting scheduled auto-evolution cycle at {start_time}")
        
        try:
            # Update ingestion pipeline to include synthesis if configured
            # Always runs stage="all" for the full cycle
            self.coordinator.run_ingestion_pipeline(
                stage="all",
                synthesis=self.synthesis, 
                max_samples=self.max_samples
            )
            
            # Followed by training
            self.coordinator.run_training_pipeline()
            
            end_time = datetime.now()
            duration = end_time - start_time
            logger.info(f"Auto-evolution cycle completed successfully. Duration: {duration}")
        except Exception as e:
            logger.error(f"Error during auto-evolution cycle: {e}")
        finally:
            self._is_running = False
            next_run = datetime.now() + timedelta(hours=24)
            logger.info(f"Next evolution cycle scheduled for: {next_run}")

    def start(self, interval_hours: int = 24, synthesis: bool = False, max_samples: int = None):
        """Start the blocking scheduler."""
        self.synthesis = synthesis
        self.max_samples = max_samples
        
        logger.info(f"Agent S started. Auto-evolution every {interval_hours} hours (Synthesis: {synthesis}).")
        
        # Trigger the first run immediately
        self.scheduler.add_job(
            self.scheduled_task,
            trigger=IntervalTrigger(hours=interval_hours),
            id='auto_evolution_job',
            next_run_time=datetime.now() # Run immediately on start
        )
        
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Agent S stopping...")
            self.scheduler.shutdown()

