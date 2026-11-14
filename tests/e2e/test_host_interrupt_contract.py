"""The agent's 'the host stopped this job' codes are the ones the API retries for free.

If either side renames a code, a stopped job would be recorded as the customer's failure instead of being re-run on
another computer. This test ties the two together.
"""

from __future__ import annotations

from app.services.leases import HOST_CAUSED_ERROR_CODES
from computecoin_agent.interrupts import HOST_INTERRUPTED_CODE


def test_the_agents_host_interrupted_code_is_one_the_api_treats_as_the_hosts_doing():
    assert HOST_INTERRUPTED_CODE in HOST_CAUSED_ERROR_CODES


def test_the_agents_thermal_shutdown_code_is_too():
    # executor.py reports an overheating stop with this literal code.
    import inspect

    from computecoin_agent import executor

    assert '"thermal_shutdown"' in inspect.getsource(executor)
    assert "thermal_shutdown" in HOST_CAUSED_ERROR_CODES
