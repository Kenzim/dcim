"""``test_recurring_billing_service.py`` in each tax engine mode (design 0, 15.3).

Every test of that file is collected again here with the tax engine in
``legacy``, ``shadow`` and ``v2`` (with the switch record, tax enabled, a GB
registration and a 20% rule), so the renewal worker behaves the same in all
three. Reseller renewals are outside the scope of tax by default (4.6).
"""

from __future__ import annotations

import pytest

from tests.services.tax_helpers import add_registration, add_rule, enable_tax, seed_classes, set_engine
from tests.services.test_recurring_billing_service import *  # noqa: F403

# Fails on the base branch for a reason unrelated to tax (the one accepted
# failure); collected once in its own file, not three more times here.
del test_outbox_dedupe_disabled_and_retry  # noqa: F821


@pytest.fixture(autouse=True, params=["legacy", "shadow", "v2"])
def _tax_mode(request, db_session):
    set_engine(db_session, "tax", request.param)
    if request.param == "v2":
        seed_classes(db_session)
        enable_tax(db_session, "GB")
        add_registration(db_session, "GB")
        add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack")
    db_session.commit()
    return request.param
