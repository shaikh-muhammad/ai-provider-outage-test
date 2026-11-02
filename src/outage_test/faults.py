"""Deterministic S0–S3 decisions; callers supply monotonic elapsed seconds.

Execution ordinals are one-based actual executions, never HTTP attempt counts.
An idempotent replay must not advance this ordinal. S1 depends on arrival timing
and bucket state; there is deliberately no provider-wide exact failure trace.
Defaults remain provisional until G7. Inputs here are internal typed values;
HTTP/configuration schema validation belongs to the future boundary layers.
"""

import hashlib
import json
import math


def _number(value, name, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def _draw(seed, scenario, job_id, execution_ordinal, purpose):
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("seed must be an unsigned 64-bit integer")
    if type(scenario) is not str or scenario not in ("S0", "S1", "S2", "S3"):
        raise ValueError("invalid scenario")
    if type(job_id) is not str or not 1 <= len(job_id) <= 256:
        raise ValueError("job_id must contain 1–256 characters")
    if type(execution_ordinal) is not int or not 1 <= execution_ordinal < 2**64:
        raise ValueError("execution_ordinal must be a positive 64-bit integer")
    data = json.dumps([seed, scenario, job_id, execution_ordinal, purpose])
    bits = int.from_bytes(hashlib.sha256(data.encode()).digest()[:8], "big")
    return (bits >> 11) / 2**53


class TokenBucket:
    """Start full at supplied monotonic time; rejected arrivals spend nothing."""

    def __init__(self, rate=10.0, capacity=10.0, now=0.0):
        self.rate = _number(rate, "rate")
        self.capacity = _number(capacity, "capacity", 1)
        self.tokens = self.capacity
        self.last = _number(now, "now")

    def allow(self, now):
        now = _number(now, "now", self.last)
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def fault(seed, scenario, job_id, execution_ordinal, now, *, bucket=None,
          outage_start=20.0, outage_end=35.0, loss_fraction=0.15):
    """Return HEALTHY, RATE_LIMITED, ERROR_503, HUNG, or LOST_REPLY.

    S2 partitions stable per-execution hashes equally between 503 and hung.
    S3 selects job membership using ordinal 1, then loses only that execution.
    S1 requires a per-run bucket and monotonic elapsed arrival time.
    """
    draw = _draw(seed, scenario, job_id, execution_ordinal, "fault")
    _number(now, "now")
    _number(outage_start, "outage_start")
    _number(outage_end, "outage_end", outage_start)
    if _number(loss_fraction, "loss_fraction") > 1:
        raise ValueError("loss_fraction must be at most 1")
    if scenario == "S1":
        if type(bucket) is not TokenBucket:
            raise ValueError("S1 requires a TokenBucket")
        return "HEALTHY" if bucket.allow(now) else "RATE_LIMITED"
    if scenario == "S2" and outage_start <= now < outage_end:
        return "ERROR_503" if draw < 0.5 else "HUNG"
    if scenario == "S3" and execution_ordinal == 1 and draw < loss_fraction:
        return "LOST_REPLY"
    return "HEALTHY"


def service_time(seed, scenario, job_id, execution_ordinal, base=0.080):
    """Seconds in [0.9 * base, 1.1 * base), with a separate hash domain."""
    _number(base, "base")
    return base * (0.9 + 0.2 * _draw(
        seed, scenario, job_id, execution_ordinal, "service"))
