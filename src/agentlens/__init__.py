"""agentlens: every Claude Code session you have open, on one screen in your terminal."""

import time

STARTED = time.perf_counter()  # when agentlens's code began loading: startup timings count from here


def __getattr__(name: str):
    if name == "__version__":  # asked for, not worked out on import: from a checkout, it asks git
        from agentlens.version import number

        return number()
    raise AttributeError(f"module 'agentlens' has no attribute {name!r}")
