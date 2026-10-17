from celery import Celery

from app.core.config import settings

celery_app = Celery("voxai", broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND,
                    include=["app.workers.tasks"])
celery_app.conf.update(
    task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    task_default_retry_delay=5, timezone="UTC",
    beat_schedule={
        "poll-crm-reconciliation": {"task": "app.workers.tasks.reconcile_all_crms", "schedule": 300.0},
        "dispatch-due-call-jobs": {"task": "app.workers.tasks.dispatch_due_call_jobs", "schedule": 15.0},
        "enqueue-due-callbacks": {"task": "app.workers.tasks.enqueue_due_callbacks", "schedule": 60.0},
    },
)
