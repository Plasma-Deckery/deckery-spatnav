#!/usr/bin/env python3
"""
bench_render.py — before/after benchmark for the render_tree() hot path
(deckery#28 extents-cache work).

Uses the real ElementCache + render_tree() against whatever is actually on
screen right now. Two numbers matter:

  1. render_tree() elapsed_ms across repeated calls (WalkResult.elapsed_ms,
     already tracked in production and shown in the HUD).
  2. Number of AXComponent.get_rect() calls per render — instrumented here
     via a counting wrapper around Atspi.Component.get_extents, the actual
     D-Bus-facing call get_rect() makes (vendor/orca_ax/ax_component.py).

Also runs a one-off micro-benchmark: calls get_rect() on the SAME element
many times back-to-back and reports per-call latency. This settles whether
get_rect() is actually served from a cache (microseconds/call) or is a live
D-Bus round-trip (milliseconds/call) — the docstrings in deckery_cache.py
and deckery_walk.py assert the former; this measures it directly instead of
trusting the comment.

Usage: distrobox enter deckery -- python3 bench_render.py [N_RENDERS] [DELAY_MS]

DELAY_MS (default 0) sleeps between renders to approximate real usage: in
production, render_tree() runs once per debounced GLib-idle tick (multiple
raw AT-SPI events coalesce into one render — deckery_events.py), NOT
back-to-back as fast as Python can loop. DELAY_MS=0 is a worst-case stress
test for the cache (each render must wait for the previous one's full
duration before starting, which can itself exceed the TTL); a small delay
(e.g. 50) is closer to a real burst of closely-spaced but independently
triggered renders.
"""

import statistics
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

import deckery_cache
import deckery_walk
from vendor.orca_ax.ax_component import AXComponent
from vendor.orca_ax.ax_object import AXObject

N_RENDERS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
DELAY_MS = int(sys.argv[2]) if len(sys.argv) > 2 else 0


def install_call_counter():
    """Wraps the three underlying D-Bus-facing GI calls that
    get_element_snapshot() bundles (Atspi.Component.get_extents,
    Atspi.Accessible.get_name, Atspi.Accessible.get_role_name) to count
    calls without touching vendored Orca code. Returns (counts, restore_fn)."""
    counts = {"get_extents": 0, "get_name": 0, "get_role_name": 0}
    originals = {
        "get_extents": Atspi.Component.get_extents,
        "get_name": Atspi.Accessible.get_name,
        "get_role_name": Atspi.Accessible.get_role_name,
    }

    def counting_get_extents(obj, coord_type):
        counts["get_extents"] += 1
        return originals["get_extents"](obj, coord_type)

    def counting_get_name(obj):
        counts["get_name"] += 1
        return originals["get_name"](obj)

    def counting_get_role_name(obj):
        counts["get_role_name"] += 1
        return originals["get_role_name"](obj)

    Atspi.Component.get_extents = counting_get_extents
    Atspi.Accessible.get_name = counting_get_name
    Atspi.Accessible.get_role_name = counting_get_role_name

    def restore():
        Atspi.Component.get_extents = originals["get_extents"]
        Atspi.Accessible.get_name = originals["get_name"]
        Atspi.Accessible.get_role_name = originals["get_role_name"]

    return counts, restore


def micro_benchmark_repeat_calls(cache, n=500):
    """Calls get_rect() n times on the same element back-to-back, reports
    per-call latency. Distinguishes a real cache hit (µs) from a live D-Bus
    round-trip (ms)."""
    sample_element = None
    for wm in cache.windows:
        elems = cache.elements_for(hash(wm.window))
        if elems:
            sample_element = elems[0]
            break
    if sample_element is None:
        print("[bench] no element available for micro-benchmark")
        return

    t0 = time.perf_counter()
    for _ in range(n):
        AXComponent.get_rect(sample_element)
    t1 = time.perf_counter()
    per_call_us = (t1 - t0) / n * 1_000_000
    print(f"[bench] micro: {n}x get_rect() on the SAME element: "
          f"{per_call_us:.1f} µs/call total ({(t1 - t0) * 1000:.1f} ms for {n} calls)")
    if per_call_us > 200:
        print("[bench]   -> consistent with a LIVE D-Bus round-trip per call, not a cache hit")
    else:
        print("[bench]   -> consistent with an in-process cache hit")

    for label, fn in (
        ("get_name", AXObject.get_name),
        ("get_role_name", AXObject.get_role_name),
    ):
        t0 = time.perf_counter()
        for _ in range(n):
            fn(sample_element)
        t1 = time.perf_counter()
        per_call_us = (t1 - t0) / n * 1_000_000
        verdict = "LIVE D-Bus round-trip" if per_call_us > 200 else "in-process cache hit"
        print(f"[bench] micro: {n}x {label}() on the SAME element: "
              f"{per_call_us:.1f} µs/call  -> consistent with {verdict}")


def main():
    print("[bench] building ElementCache via build_initial() (real production path)...")
    cache = deckery_cache.ElementCache()
    t0 = time.perf_counter()
    meta, elements = cache.build_initial()
    cache.apply_initial(meta, elements)
    build_ms = (time.perf_counter() - t0) * 1000
    n_windows = len(meta)
    n_elements = sum(len(v) for v in elements.values())
    print(f"[bench] build_initial(): {build_ms:.0f} ms, {n_windows} windows, {n_elements} elements")

    micro_benchmark_repeat_calls(cache)

    counts, restore = install_call_counter()

    # Minimal tracker stand-in: render_tree() only reads .visible_pids (None
    # means "unfiltered", matching the real fallback-without-tracker path) and
    # the various offset/caption maps via deckery_filter.py, which all
    # degrade to empty-dict lookups when tracker state is empty — same
    # behavior as a freshly-started overlay before the first KWin Update().
    class _NullTracker:
        visible_pids = None
        visible_captions_by_pid = {}
        all_captions_by_pid = {}
        all_known_pids = set()
        caption_visible_global = {}
        caption_offset_global = {}
        client_offset_by_key = {}
        client_offset_by_pid = {}
        client_offset_by_pid_size = {}
        hidden_pid_size_keys = set()

    tracker = _NullTracker()

    elapsed_list = []
    calls_list = []
    for i in range(N_RENDERS):
        if i > 0 and DELAY_MS:
            time.sleep(DELAY_MS / 1000.0)
        for k in counts:
            counts[k] = 0
        result = deckery_walk.render_tree(tracker, cache)
        elapsed_list.append(result.elapsed_ms)
        calls_list.append(sum(counts.values()))

    restore()

    cold_elapsed, cold_calls = elapsed_list[0], calls_list[0]
    warm_elapsed = elapsed_list[1:]
    warm_calls = calls_list[1:]

    print(f"\n[bench] {N_RENDERS} renders (delay={DELAY_MS}ms between) against "
          f"{n_windows} windows / {n_elements} cached elements:")
    print(f"  render #1 (cold — nothing cached yet):"
          f"  {cold_elapsed:.1f} ms, {cold_calls} D-Bus calls "
          f"(get_rect+get_name+get_role_name combined)")
    if warm_elapsed:
        print(f"  renders #2-{N_RENDERS} (each within TTL of the previous):")
        print(f"    elapsed_ms  min={min(warm_elapsed):.2f}  "
              f"median={statistics.median(warm_elapsed):.2f}  "
              f"max={max(warm_elapsed):.2f}  "
              f"mean={statistics.mean(warm_elapsed):.2f}")
        print(f"    D-Bus calls  min={min(warm_calls)}  "
              f"median={statistics.median(warm_calls):.0f}  "
              f"max={max(warm_calls)}")
    print(f"  elements actually drawn (last render): {len(result.elements)}")


if __name__ == "__main__":
    main()
