"""Every call in app/api/client.py must bind to the signature of the function it calls."""

import ast
import inspect
from pathlib import Path

import app.api.client as client_module

CLIENT_SOURCE = Path(client_module.__file__)


def _awaited_calls():
    tree = ast.parse(CLIENT_SOURCE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Name):
                yield call


def test_wrapped_handlers_are_called_with_matching_arguments():
    checked = 0
    for call in _awaited_calls():
        target = getattr(client_module, call.func.id, None)
        if target is None or not inspect.iscoroutinefunction(target):
            continue
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            continue
        args = [object()] * len(call.args)
        kwargs = {k.arg: object() for k in call.keywords}
        try:
            inspect.signature(target).bind(*args, **kwargs)
        except TypeError as exc:
            raise AssertionError(
                f"app/api/client.py:{call.lineno} calls {call.func.id} with arguments "
                f"that do not match its signature: {exc}"
            ) from exc
        checked += 1
    assert checked > 0
