"""Test-session hooks for neptune-context."""

import os
from collections.abc import Iterator

import pytest

# In CI, an unmarked test slower than SLOW_BUDGET_S fails: mark it `slow` (the nightly runs it).
SLOW_BUDGET_S = 60.0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    if (
        os.environ.get("CI")
        and report.when == "call"
        and report.passed
        and "slow" not in item.keywords
        and report.duration > SLOW_BUDGET_S
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} took {report.duration:.0f} s, over the {SLOW_BUDGET_S:.0f} s budget "
            "for an unmarked test: mark it @pytest.mark.slow"
        )
