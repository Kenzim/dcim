"""RADOS lease client of the Rackflow storage runner.

One process is one RADOS client instance. The runner sends one JSON request
per line on stdin, {"id": N, "op": "...", ...}, and reads one JSON reply per
line on stdout, {"id": N, "ok": true, "result": ...} or
{"id": N, "ok": false, "errno": E, "error": "..."}. The first line written is
{"ready": true, "client": ..., "addr": ...} once connected, or
{"ready": false, ...} before exiting. Requests run on a small thread pool, so
one slow object does not hold up the others.
"""

import base64
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import rados

LOCK_NAME = "owner"
LOCK_DESC = "rackflow lease"
READ_LIMIT = 1 << 20


class HelperError(Exception):
    def __init__(self, err, message):
        super().__init__(message)
        self.errno = err


def emit(lock, msg):
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    with lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def connect(cfg):
    conf = {
        "rados_osd_op_timeout": str(cfg.get("op_timeout", 8)),
        "rados_mon_op_timeout": str(cfg.get("op_timeout", 8)),
        "client_mount_timeout": str(cfg.get("op_timeout", 8)),
    }
    if cfg.get("keyring"):
        conf["keyring"] = cfg["keyring"]
    cluster = rados.Rados(conffile=cfg.get("conf") or "", name=cfg.get("name") or None, conf=conf)
    cluster.connect()
    return cluster


def mon(cluster, cmd):
    ret, out, outs = cluster.mon_command(json.dumps(cmd), b"")
    if ret != 0:
        raise HelperError(-ret if ret < 0 else ret, outs or "mon command failed")
    return out


def rados_cli(cfg, args):
    cmd = ["rados", "--format=json", "-p", cfg["pool"], "-N", cfg["namespace"]]
    if cfg.get("conf"):
        cmd += ["-c", cfg["conf"]]
    if cfg.get("name"):
        cmd += ["-n", cfg["name"]]
    if cfg.get("keyring"):
        cmd += ["--keyring", cfg["keyring"]]
    try:
        p = subprocess.run(cmd + args, capture_output=True, timeout=cfg.get("op_timeout", 8))
    except subprocess.TimeoutExpired:
        raise HelperError(110, "rados %s timed out" % args[0])
    if p.returncode != 0:
        msg = p.stderr.decode("utf-8", "replace").strip()
        err = 2 if "No such file" in msg else (16 if "busy" in msg.lower() else p.returncode)
        raise HelperError(err, msg or "rados %s failed" % args[0])
    return p.stdout


def make_ops(cluster, ioctx, cfg):
    pool = cfg["pool"]

    def lock_shared(r):
        ioctx.lock_shared(r["oid"], LOCK_NAME, r["cookie"], r["tag"], desc=LOCK_DESC,
                          duration=int(r["duration"]), flags=int(r.get("flags", 0)))

    def unlock(r):
        ioctx.unlock(r["oid"], LOCK_NAME, r["cookie"])

    def break_lock(r):
        if hasattr(ioctx, "break_lock"):
            ioctx.break_lock(r["oid"], LOCK_NAME, r["client"], r["cookie"])
            return
        rados_cli(cfg, ["lock", "break", r["oid"], LOCK_NAME, r["client"], "--lock-cookie", r["cookie"]])

    def list_lockers(r):
        if not hasattr(ioctx, "list_lockers"):
            info = json.loads(rados_cli(cfg, ["lock", "info", r["oid"], LOCK_NAME]) or b"{}")
            return {
                "tag": info.get("tag") or "",
                "exclusive": info.get("type") == "exclusive",
                "lockers": [{"client": e.get("name", ""), "cookie": e.get("cookie", ""), "addr": e.get("addr", "")}
                            for e in info.get("lockers", [])],
            }
        info = ioctx.list_lockers(r["oid"], LOCK_NAME)
        return {
            "tag": info.get("tag") or "",
            "exclusive": bool(info.get("exclusive")),
            "lockers": [{"client": c, "cookie": k, "addr": a} for c, k, a in info.get("lockers", [])],
        }

    def get_xattrs(r):
        return {k: v.decode("utf-8", "replace") for k, v in ioctx.get_xattrs(r["oid"])}

    def set_xattr(r):
        ioctx.set_xattr(r["oid"], r["name"], r["value"].encode("utf-8"))

    def write_full(r):
        ioctx.write_full(r["oid"], base64.b64decode(r["data"]))

    def read(r):
        return base64.b64encode(ioctx.read(r["oid"], length=READ_LIMIT)).decode("ascii")

    def remove(r):
        ioctx.remove_object(r["oid"])

    def purge(r):
        other = cluster.open_ioctx(pool)
        try:
            other.set_namespace(r["ns"])
            keys = [o.key for o in other.list_objects()]
            for key in keys:
                try:
                    other.remove_object(key)
                except rados.ObjectNotFound:
                    pass
            return len(keys)
        finally:
            other.close()

    def blocklist_add(r):
        mon(cluster, {"prefix": "osd blocklist", "blocklistop": "add",
                      "addr": r["addr"], "expire": float(r["expire"])})

    def blocklist_ls(r):
        out = mon(cluster, {"prefix": "osd blocklist ls", "format": "json"})
        data = json.loads(out or b"[]")
        if isinstance(data, dict):
            data = data.get("blocklist", [])
        return [{"addr": e.get("addr", ""), "until": e.get("until", "")} for e in data]

    return {
        "lock_shared": lock_shared,
        "unlock": unlock,
        "break_lock": break_lock,
        "list_lockers": list_lockers,
        "get_xattrs": get_xattrs,
        "set_xattr": set_xattr,
        "write_full": write_full,
        "read": read,
        "remove": remove,
        "purge": purge,
        "blocklist_add": blocklist_add,
        "blocklist_ls": blocklist_ls,
    }


def handle(lock, ops, req):
    rid = req.get("id")
    try:
        op = ops.get(req.get("op"))
        if op is None:
            raise HelperError(22, "unknown op %r" % req.get("op"))
        emit(lock, {"id": rid, "ok": True, "result": op(req)})
    except (rados.Error, HelperError) as e:
        emit(lock, {"id": rid, "ok": False, "errno": getattr(e, "errno", None) or 0, "error": str(e)})
    except Exception as e:  # noqa: BLE001 - every failure must reach the runner
        emit(lock, {"id": rid, "ok": False, "errno": 0, "error": "%s: %s" % (type(e).__name__, e)})


def main():
    lock = threading.Lock()
    cfg = json.loads(sys.argv[1])
    try:
        cluster = connect(cfg)
        ioctx = cluster.open_ioctx(cfg["pool"])
        ioctx.set_namespace(cfg["namespace"])
        client = "client.%d" % cluster.get_instance_id()
        addr = cluster.get_addrs()
    except Exception as e:  # noqa: BLE001
        emit(lock, {"ready": False, "errno": getattr(e, "errno", None) or 0, "error": str(e)})
        os._exit(1)
    emit(lock, {"ready": True, "client": client, "addr": addr})
    ops = make_ops(cluster, ioctx, cfg)
    pool = ThreadPoolExecutor(max_workers=16)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as e:
            emit(lock, {"id": None, "ok": False, "errno": 22, "error": "bad request: %s" % e})
            continue
        pool.submit(handle, lock, ops, req)
    os._exit(0)


if __name__ == "__main__":
    main()
