from app.core import redis_notifications


def test_setup_keyspace_notifications_calls_redis_config(monkeypatch):
    calls = []

    class DummyRedis:
        def config_set(self, key, value):
            calls.append((key, value))

    monkeypatch.setattr(redis_notifications, "redis_client", DummyRedis())
    redis_notifications.setup_keyspace_notifications()

    assert calls == [("notify-keyspace-events", "Ex")]


def test_setup_keyspace_notifications_handles_redis_errors(monkeypatch):
    class DummyRedis:
        def config_set(self, key, value):
            raise RuntimeError("boom")

    monkeypatch.setattr(redis_notifications, "redis_client", DummyRedis())
    redis_notifications.setup_keyspace_notifications()


def test_start_keyspace_notification_listener_starts_daemon_thread(monkeypatch):
    started = {"value": False}

    class DummyThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            started["value"] = True

    monkeypatch.setattr("threading.Thread", DummyThread)
    redis_notifications.start_keyspace_notification_listener()

    assert started["value"] is True
