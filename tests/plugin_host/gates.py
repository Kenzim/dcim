"""A gate that test plugin handlers block on until the test opens it (tests/plugin_host/test_invoke_slots.py)."""

import threading

RELEASE = threading.Event()
ENTERED = threading.Semaphore(0)


def hold() -> None:
    """Count the entry, then block until the test sets RELEASE (at most 30 seconds)."""
    ENTERED.release()
    RELEASE.wait(30)


def reset() -> None:
    global ENTERED
    RELEASE.clear()
    ENTERED = threading.Semaphore(0)


def entered(count: int, seconds: float = 5.0) -> bool:
    """Wait until ``count`` more handlers have entered the gate."""
    return all(ENTERED.acquire(timeout=seconds) for _ in range(count))
