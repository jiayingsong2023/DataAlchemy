"""
Agent S: The Scheduler.
Responsible for periodic tasks and system auto-evolution.
"""
import time
import logging
import sys
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
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
        self.scheduler = BackgroundScheduler()
        self._is_running = False
        self.synthesis = False
        self.max_samples = None

    def scheduled_task(self, is_manual=False):
        """The task that runs every 24 hours."""
        if self._is_running:
            logger.warning("Previous task is still running. Skipping this cycle.")
            return
            
        self._is_running = True
        start_time = datetime.now()
        mode_str = "MANUAL" if is_manual else "SCHEDULED"
        logger.info(f"Starting {mode_str} auto-evolution cycle at {start_time}")
        
        try:
            # Update ingestion pipeline to include synthesis if configured
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
            # CRITICAL: Release all AI agents and GPU memory after work
            self.coordinator.clear_agents()
            self._is_running = False
            
            if not is_manual:
                next_run = self.scheduler.get_job('auto_evolution_job').next_run_time
                logger.info(f"Next evolution cycle scheduled for: {next_run}")

    def start(self, interval_hours: int = 24, synthesis: bool = False, max_samples: int = None):
        """Start the background scheduler and enter interactive mode."""
        self.synthesis = synthesis
        self.max_samples = max_samples
        
        logger.info(f"Agent S started. Auto-evolution every {interval_hours} hours.")
        
        self.scheduler.add_job(
            self.scheduled_task,
            trigger=IntervalTrigger(hours=interval_hours),
            id='auto_evolution_job',
            next_run_time=datetime.now() # First run immediately
        )
        
        self.scheduler.start()
        
        print("\n" + "*" * 60)
        print("  AGENT S INTERACTIVE CONSOLE")
        print("  - Press [Enter] to check status")
        print("  - Type 'now' to trigger an immediate run")
        print("  - Type 'quit' or 'exit' to stop the scheduler")
        print("*" * 60 + "\n")

        try:
            # Check if running in a container/non-interactive env
            if not sys.stdin.isatty():
                logger.info("[AGENT S] Non-interactive environment detected. Entering sleep loop.")
                while True:
                    time.sleep(3600)
            
            while True:
                cmd = input("AgentS > ").strip().lower()
                
                if cmd in ['quit', 'exit']:
                    logger.info("Agent S stopping...")
                    self.scheduler.shutdown()
                    break
                elif cmd == 'now':
                    if not self._is_running:
                        print("[!] Manual trigger received.")
                        self.scheduled_task(is_manual=True)
                    else:
                        print("[!] A cycle is already in progress.")
                else:
                    # Just status update on Enter
                    job = self.scheduler.get_job('auto_evolution_job')
                    if job:
                        status = "WORKING" if self._is_running else "IDLE"
                        print(f"--- Status: {status} | Next run: {job.next_run_time} ---")
                    
        except (KeyboardInterrupt, SystemExit, EOFError):
            logger.info("Agent S stopping...")
            self.scheduler.shutdown()
