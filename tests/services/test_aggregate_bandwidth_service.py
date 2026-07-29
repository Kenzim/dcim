"""Unit tests for aggregate monitored-port bandwidth helpers."""
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

from app.services.aggregate_bandwidth_service import (
    aggregate_in_out,
    bucket_span_ms,
    rates_from_samples,
)


def test_bucket_span_ms_targets_about_96_points():
    span = bucket_span_ms(24)
    assert span == 900_000  # 24h / 96 = 15 min
    assert bucket_span_ms(1) >= 60_000


def test_rates_from_samples_computes_mbps():
    t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=10)
    samples = [
        SimpleNamespace(sampled_at=t0, bytes_in=0, bytes_out=0),
        # 12_500_000 bytes in 10s => 10 Mbps; 25_000_000 => 20 Mbps
        SimpleNamespace(sampled_at=t1, bytes_in=12_500_000, bytes_out=25_000_000),
    ]
    points = rates_from_samples(samples)
    assert len(points) == 1
    assert abs(points[0]["rate_in_mbps"] - 10.0) < 0.01
    assert abs(points[0]["rate_out_mbps"] - 20.0) < 0.01


def test_rates_from_samples_handles_counter_wrap():
    t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=1)
    samples = [
        SimpleNamespace(sampled_at=t0, bytes_in=1000, bytes_out=2000),
        SimpleNamespace(sampled_at=t1, bytes_in=100, bytes_out=50),
    ]
    points = rates_from_samples(samples)
    assert len(points) == 1
    # After wrap, deltas use the new absolute counter values.
    assert points[0]["rate_in_mbps"] == 100 * 8 / 1_000_000
    assert points[0]["rate_out_mbps"] == 50 * 8 / 1_000_000


def test_aggregate_in_out_sums_ports_into_separate_series():
    span = 60_000
    t = 1_700_000_000_000
    port_a = [{"t": t + 1000, "rate_in_mbps": 1.5, "rate_out_mbps": 0.5}]
    port_b = [{"t": t + 2000, "rate_in_mbps": 2.5, "rate_out_mbps": 1.5}]
    in_samples, out_samples = aggregate_in_out([port_a, port_b], span)
    assert len(in_samples) == 1
    assert len(out_samples) == 1
    assert in_samples[0]["value"] == 4.0
    assert out_samples[0]["value"] == 2.0
    assert in_samples[0]["t"] == out_samples[0]["t"]
