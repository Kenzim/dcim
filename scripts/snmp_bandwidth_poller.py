#!/usr/bin/env python3
"""
Standalone SNMP bandwidth poller.

Polls all enabled network switches that support MONITORING (e.g. SNMPv3 plugin),
collects IF-MIB octet counters per port, and stores them in switch_bandwidth_samples.

Run as a long-lived process (e.g. systemd service) or from cron at a fixed interval.
Uses the same DATABASE_URL and app plugins as the main DCIM app.

Usage:
  python -m scripts.snmp_bandwidth_poller [--interval 60] [--once]
  # or
  ./scripts/snmp_bandwidth_poller.py --interval 60

Environment:
  DATABASE_URL  - Required. Same as main app (e.g. postgresql://...)
  POLL_INTERVAL - Optional. Seconds between poll cycles (default 60).
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.dao.network_switch_dao import NetworkSwitchDAO
from app.dao.switch_bandwidth_sample_dao import SwitchBandwidthSampleDAO
from app.plugins.switch_base import SwitchPluginCategory
from app.plugins.switch_registry import get_switch_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("snmp_bandwidth_poller")


def _switches_with_monitoring(db) -> List[Any]:
    """Return enabled switches whose plugin supports MONITORING (get_all_port_statistics)."""
    registry = get_switch_registry()
    all_switches = NetworkSwitchDAO.get_all(db, limit=500, enabled_only=True)
    return [sw for sw in all_switches if registry.supports_monitoring(sw.plugin_name)]


async def _poll_switch(switch: Any) -> List[Dict[str, Any]]:
    """Poll one switch via its plugin; return list of sample dicts for DAO.create_many."""
    registry = get_switch_registry()
    try:
        plugin = registry.get_plugin(switch.plugin_name, switch.plugin_config or {})
    except KeyError:
        logger.warning("Plugin not found for switch %s: %s", switch.name, switch.plugin_name)
        return []
    if SwitchPluginCategory.MONITORING not in plugin.SUPPORTED_CATEGORIES:
        return []
    try:
        stats = await plugin.get_all_port_statistics()
    except Exception as e:
        logger.warning("Failed to get port statistics for switch %s (%s): %s", switch.name, switch.plugin_name, e)
        return []
    now = datetime.now(timezone.utc)
    samples = []
    for port_key, data in stats.items():
        samples.append({
            "switch_id": switch.id,
            "port_identifier": port_key,
            "if_index": data.get("ifIndex"),
            "bytes_in": int(data.get("ifInOctets") or 0),
            "bytes_out": int(data.get("ifOutOctets") or 0),
            "in_errors": int(data.get("ifInErrors") or 0),
            "out_errors": int(data.get("ifOutErrors") or 0),
            "in_discards": int(data.get("ifInDiscards") or 0),
            "out_discards": int(data.get("ifOutDiscards") or 0),
            "sampled_at": now,
        })
    return samples


async def poll_all_switches(switches: List[Any]) -> List[Dict[str, Any]]:
    """Poll all switches concurrently and return combined list of samples."""
    if not switches:
        return []
    tasks = [_poll_switch(sw) for sw in switches]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_samples = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.warning("Poll failed for switch %s: %s", switches[i].name, r)
            continue
        all_samples.extend(r)
    return all_samples


def run_one_cycle() -> int:
    """Load switches, poll via async, insert samples. Returns number of samples stored."""
    db = SessionLocal()
    try:
        switches = _switches_with_monitoring(db)
        if not switches:
            logger.info("No switches with monitoring support enabled; skipping cycle.")
            return 0
        logger.info("Polling %d switch(es) for bandwidth data.", len(switches))
        samples = asyncio.run(poll_all_switches(switches))
        if not samples:
            logger.info("No samples collected this cycle.")
            return 0
        SwitchBandwidthSampleDAO.create_many(db, samples)
        logger.info("Stored %d bandwidth samples.", len(samples))
        return len(samples)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll SNMP bandwidth data and store in DB")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL", "60")),
        help="Seconds between poll cycles (default: 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit (e.g. for cron)",
    )
    args = parser.parse_args()
    if args.once:
        run_one_cycle()
        return
    logger.info("Starting SNMP bandwidth poller (interval=%ds).", args.interval)
    while True:
        try:
            run_one_cycle()
        except Exception as e:
            logger.exception("Poll cycle failed: %s", e)
        try:
            import time
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Stopping poller.")
            break


if __name__ == "__main__":
    main()
