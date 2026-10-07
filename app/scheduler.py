from apscheduler.schedulers.background import BackgroundScheduler


def create_scheduler() -> BackgroundScheduler:
    """Background scheduler for future ingestion jobs. No jobs are registered yet."""
    return BackgroundScheduler(timezone="UTC")
