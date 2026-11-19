import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stormchaser.models import Tick  # noqa: E402
from stormchaser.score import StormConfig, StormEngine  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "stormchaser" / "data"


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def walk(n, sigma=0.001, start=100.0, seed=1, t0=1_700_000_000.0, step=60.0, drift=0.0):
    """Seeded geometric random walk as a list of ticks."""
    rng = np.random.default_rng(seed)
    px = start * np.exp(np.cumsum(rng.normal(drift, sigma, n)))
    return [Tick(t0 + i * step, float(p)) for i, p in enumerate(px)]


def extend(ticks, n, sigma=0.001, seed=2, drift=0.0, step=60.0, gap=0.0):
    rng = np.random.default_rng(seed)
    px = ticks[-1].price * np.exp(np.cumsum(rng.normal(drift, sigma, n)))
    t0 = ticks[-1].ts + gap
    return ticks + [Tick(t0 + (i + 1) * step, float(p)) for i, p in enumerate(px)]


def run(ticks, cfg=None, clock=None, **kw):
    eng = StormEngine("TEST", cfg or StormConfig(**kw), clock=clock or FakeClock())
    levels = []
    for t in ticks:
        eng.update(t)
        levels.append(eng.level)
    return eng, levels


@pytest.fixture
def clock():
    return FakeClock()


def finite_tree(o):
    """True if every number in a nested payload is finite."""
    if isinstance(o, dict):
        return all(finite_tree(v) for v in o.values())
    if isinstance(o, (list, tuple)):
        return all(finite_tree(v) for v in o)
    if isinstance(o, float):
        return math.isfinite(o)
    return True


SGX_FILES = [DATA / f"sgx_{p}_2026.parquet" for p in ("usdjpy", "eurusd", "usdsgd")]
SGX_TESTS = ("sgx", "tornado_freeze_carries_hindsight", "playbook_ideas_follow_the_level", "bundled_datasets_load")


def pytest_collection_modifyitems(config, items):
    """The raw SGX challenge files are not redistributed; skip their tests when absent."""
    if all(f.exists() for f in SGX_FILES):
        return
    skip = pytest.mark.skip(reason="SGX challenge data not bundled: run scripts/build_sgx.py on Challenge1.zip")
    for item in items:
        if any(k in item.name for k in SGX_TESTS):
            item.add_marker(skip)
