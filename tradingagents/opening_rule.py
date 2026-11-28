"""Frozen 09:35 exposure rule, shared by research replay and forward observation."""
import math

STRATEGY = "opening0935-v1"
REFERENCE_CAPITAL = 7000
ORDER_FEE = 5


def opening_target(current, previous_vxn, reference, can_reduce, warning_ratio=.90):
    if not all(math.isfinite(x) and x > 0 for x in (previous_vxn, reference)):
        raise ValueError("VXN or reference median unavailable")
    ratio = previous_vxn / reference
    target = 0.0 if ratio > 1 else 0.5 if ratio >= warning_ratio else 1.0
    if target < current and not can_reduce:
        return current
    return target
