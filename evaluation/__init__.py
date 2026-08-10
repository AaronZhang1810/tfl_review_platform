"""Offline synthetic evaluation harness for the TLF Review Platform.

The package deliberately contains no clinical data and makes no network calls.
Its simulated LLM is a deterministic behavioral stub used to exercise the
evaluation, safeguard, and reporting workflow; it is not a model benchmark.
"""

from .catalog import BENCHMARK_SEED, DISCLAIMER, FAMILIES

__all__ = ["BENCHMARK_SEED", "DISCLAIMER", "FAMILIES"]
