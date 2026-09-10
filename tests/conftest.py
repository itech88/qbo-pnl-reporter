"""Shared test fixtures.

The one thing here is a hard guarantee that the test suite can never write to the
real incident log. `scheduler.run()` flushes incidents to `docs/incident-log.md`
on the way out, so any test that exercises a full run would otherwise append
fixture data — "render boom", 600%-of-income holds — to a file that is a
production record and is committed back to the repo by CI.
"""

import pytest

import incidents


@pytest.fixture(autouse=True)
def _isolate_incident_log(tmp_path, monkeypatch):
    """Point the incident log at a throwaway file and start from an empty buffer.

    Autouse and unconditional: an opt-in fixture would be one forgotten decorator
    away from a test polluting the real log again.
    """
    monkeypatch.setattr(incidents, "LOG_PATH", str(tmp_path / "incident-log.md"))
    incidents.reset()
    yield
    incidents.reset()
