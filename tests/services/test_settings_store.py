import pytest

from app.core.config import Settings
from app.models.commerce_account import SystemSetting
from app.models.commerce_audit import UserAuditEvent
from app.services import settings_store as store
from app.services.settings_store import SettingValidationError


def _config(**overrides):
    values = {"database_url": "sqlite://"}
    values.update(overrides)
    return Settings(**values)


def _row(db, key):
    return db.get(SystemSetting, store.STORAGE_PREFIX + key)


def test_every_schema_default_resolves_and_validates():
    base = _config()
    for key, definition in store.SCHEMA.items():
        assert definition.validate(definition.default_for(base)) == definition.default_for(base), key
        assert definition.group in store.GROUPS


def test_run_recurring_billing_worker_is_env_only():
    assert "run_recurring_billing_worker" in store.ENV_ONLY
    assert "run_recurring_billing_worker" not in store.SCHEMA


def test_unknown_key_raises_key_error(db_session):
    for call in (
        lambda: store.get_setting(db_session, "nope"),
        lambda: store.set_setting(db_session, "nope", 1, None),
        lambda: store.reset_setting(db_session, "nope"),
    ):
        with pytest.raises(KeyError):
            call()


def test_default_comes_from_config_and_w0a_keys_have_fallbacks(db_session):
    base = _config(recurring_billing_grace_days=9)
    assert store.get_setting(db_session, "recurring_billing_grace_days", base=base) == 9
    assert store.get_setting(db_session, "billing_retail_renewals_enabled", base=base) is False
    assert store.get_setting(db_session, "billing_order_refund_policy", base=base) == "manual_review"


def test_set_get_reset_round_trip_with_audit(db_session):
    base = _config()
    store.set_setting(db_session, "recurring_billing_grace_days", 10, actor_user_id=None)
    db_session.commit()
    assert _row(db_session, "recurring_billing_grace_days").value == {"value": 10}
    assert store.get_setting(db_session, "recurring_billing_grace_days", base=base) == 10
    resolved = store.resolve_all(db_session, base)["recurring_billing_grace_days"]
    assert (resolved.value, resolved.default, resolved.source) == (10, 7, "stored")

    assert store.reset_setting(db_session, "recurring_billing_grace_days") is True
    db_session.commit()
    assert _row(db_session, "recurring_billing_grace_days") is None
    assert store.get_setting(db_session, "recurring_billing_grace_days", base=base) == 7
    assert store.reset_setting(db_session, "recurring_billing_grace_days") is False

    actions = [e.action for e in db_session.query(UserAuditEvent).order_by(UserAuditEvent.id)]
    assert actions == ["setting.update", "setting.reset"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("recurring_billing_grace_days", "10"),
        ("recurring_billing_grace_days", True),
        ("recurring_billing_grace_days", 1.5),
        ("recurring_billing_grace_days", 0),
        ("recurring_billing_worker_batch_size", 1001),
        ("billing_retail_renewals_enabled", 1),
        ("billing_order_refund_policy", "burn"),
        ("recurring_billing_retry_days", "3,1"),
        ("recurring_billing_retry_days", "1,x"),
        ("recurring_billing_retry_days", ""),
        ("recurring_billing_retry_days", "0,2"),
    ],
)
def test_set_rejects_invalid_values_and_writes_nothing(db_session, key, value):
    with pytest.raises(SettingValidationError):
        store.set_setting(db_session, key, value, None)
    assert _row(db_session, key) is None


def test_retry_days_are_normalised(db_session):
    assert store.set_setting(db_session, "recurring_billing_retry_days", " 1, 2 ,4 ", None) == "1,2,4"


def test_grace_must_cover_last_retry_day(db_session):
    with pytest.raises(SettingValidationError, match="last retry day"):
        store.set_setting(db_session, "recurring_billing_grace_days", 4, None)
    store.set_setting(db_session, "recurring_billing_grace_days", 20, None)
    store.set_setting(db_session, "recurring_billing_retry_days", "10,20", None)
    with pytest.raises(SettingValidationError, match="last retry day"):
        store.set_setting(db_session, "recurring_billing_retry_days", "10,25", None)


def test_topup_min_must_not_exceed_max(db_session):
    with pytest.raises(SettingValidationError, match="must not exceed"):
        store.set_setting(db_session, "reseller_topup_min_cents", 2_000_000, None)
    store.set_setting(db_session, "reseller_topup_min_cents", 100, None)


def test_invalid_stored_value_falls_back_to_default_and_logs(db_session, caplog):
    db_session.add(SystemSetting(key="setting:recurring_billing_grace_days", value={"value": "soon"}))
    db_session.add(SystemSetting(key="setting:billing_order_refund_policy", value="not-a-dict"))
    db_session.commit()
    base = _config()
    with caplog.at_level("WARNING"):
        assert store.get_setting(db_session, "recurring_billing_grace_days", base=base) == 7
        assert store.get_setting(db_session, "billing_order_refund_policy", base=base) == "manual_review"
    assert "Ignoring invalid stored setting" in caplog.text
    assert store.effective_config(db_session, base) is base


def test_stored_values_breaking_a_rule_are_ignored_on_read(db_session):
    db_session.add(SystemSetting(key="setting:recurring_billing_grace_days", value={"value": 2}))
    db_session.commit()
    base = _config()
    assert store.get_setting(db_session, "recurring_billing_grace_days", base=base) == 7


def test_foreign_keys_in_the_table_are_untouched(db_session):
    db_session.add(SystemSetting(key="alerting", value={"x": 1}))
    db_session.commit()
    store.set_setting(db_session, "recurring_billing_grace_days", 9, None)
    store.reset_setting(db_session, "recurring_billing_grace_days")
    db_session.commit()
    assert db_session.get(SystemSetting, "alerting").value == {"x": 1}


def test_effective_config_without_overrides_is_the_base_object(db_session):
    base = _config()
    assert store.effective_config(db_session, base) is base


def test_effective_config_applies_overrides_and_derived_properties(db_session):
    base = _config(smtp_port=2525)
    store.set_setting(db_session, "recurring_billing_grace_days", 14, None)
    store.set_setting(db_session, "recurring_billing_retry_days", "2,4,8", None)
    store.set_setting(db_session, "billing_retail_renewals_enabled", True, None)
    db_session.commit()
    config = store.effective_config(db_session, base)
    assert config is not base
    assert config.recurring_billing_grace_days == 14
    assert config.recurring_billing_retry_days == "2,4,8"
    assert config.recurring_billing_retry_day_offsets == (2, 4, 8)
    assert config.billing_retail_renewals_enabled is True
    assert config.smtp_port == 2525
    assert config.recurring_billing_worker_batch_size == base.recurring_billing_worker_batch_size
    assert base.recurring_billing_grace_days == 7
    assert base.recurring_billing_retry_day_offsets == (1, 3, 5)
