"""Registry of the billing jobs (docs/billing-phase2-design.md 9.1).

Each job registers here as it is built; ``app.models.billing_jobs.BILLING_JOB_NAMES``
lists the jobs that have a ``billing_job_states`` row, and
``app.services.billing.periodic.AUTOMATION_JOBS`` their cadence.
"""

from __future__ import annotations

from app.services.billing.jobs.base import BillingJob
from app.services.billing.jobs.cancellations import CancellationsJob
from app.services.billing.jobs.generate_ahead import GenerateInvoicesAhead
from app.services.billing.jobs.late_fees import LateFeesJob
from app.services.billing.jobs.reminders import InvoiceReminders
from app.services.billing.jobs.suspension import NonpaymentSuspensionJob
from app.services.billing.jobs.tax_id_recheck import TaxIdRecheck
from app.services.billing.jobs.termination import NonpaymentTerminationJob

JOBS: dict[str, BillingJob] = {
    job.name: job
    for job in (
        TaxIdRecheck(),
        GenerateInvoicesAhead(),
        InvoiceReminders(),
        LateFeesJob(),
        CancellationsJob(),
        NonpaymentSuspensionJob(),
        NonpaymentTerminationJob(),
    )
}
