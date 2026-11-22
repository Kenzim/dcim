"""Registry of the billing jobs (docs/billing-phase2-design.md 9.1).

Each job registers here as it is built; ``app.models.billing_jobs.BILLING_JOB_NAMES``
lists the jobs that have a ``billing_job_states`` row.
"""

from __future__ import annotations

from app.services.billing.jobs.base import BillingJob

JOBS: dict[str, BillingJob] = {}
