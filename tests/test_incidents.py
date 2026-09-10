"""Unit tests for incidents.py — buffering, classification and log rendering."""

import pytest

import incidents
from incidents import Incident, explain, flush, record, recorded, render, reset


@pytest.fixture(autouse=True)
def _clean_buffer():
    """The buffer is module-level global state; keep it from leaking either way."""
    reset()
    yield
    reset()


def _incident(**kw):
    base = dict(
        kind="guardrail_hold",
        summary="something went wrong",
        reasoning="because of a thing",
        remedy="held the report",
    )
    base.update(kw)
    return Incident(**base)


class TestIncident:
    def test_rejects_an_unknown_kind(self):
        with pytest.raises(ValueError, match="Unknown incident kind"):
            _incident(kind="catastrophe")

    def test_accepts_every_declared_kind(self):
        for kind in incidents.KINDS:
            assert _incident(kind=kind).kind == kind

    def test_stamps_an_aware_utc_timestamp(self):
        assert _incident().occurred_at.tzinfo is not None

    def test_defaults_to_unresolved_and_pipeline_scoped(self):
        i = _incident()
        assert i.resolved is False
        assert i.report is None


class TestBuffer:
    def test_records_in_order(self):
        record(_incident(summary="first"))
        record(_incident(summary="second"))
        assert [i.summary for i in recorded()] == ["first", "second"]

    def test_reset_empties_the_buffer(self):
        record(_incident())
        reset()
        assert recorded() == []

    def test_recorded_returns_a_copy(self):
        record(_incident())
        recorded().clear()
        assert len(recorded()) == 1

    def test_recording_never_raises(self):
        """A log that can break the run it is recording is worse than no log."""
        record(None)   # not an Incident — must be swallowed, not propagated


class TestExplain:
    def test_classifies_the_sept_1_transient_500(self):
        signal = ('500 Server Error {"errorCode":20001,"errorMessage":'
                  '"Something went wrong while processing the request"}')
        assert "throttle" in explain(signal).lower()

    def test_classifies_the_aug_16_balance_sheet_mismatch(self):
        signal = "A/R aging total 0.00 ≠ Balance Sheet A/R 50.00 (off by -50.00)"
        assert "ledger of record" in explain(signal)

    def test_classifies_detail_versus_summary_mismatch(self):
        signal = "A/R Aging detail total 50.00 ≠ aging summary 0.00"
        assert "two endpoints" in explain(signal)

    def test_classifies_invalid_grant_as_needing_a_human(self):
        assert "re-consent" in explain("error: invalid_grant")

    def test_classifies_a_renamed_account(self):
        signal = "Line item 'Home Office Supplies' not found in P&L response"
        assert "chart of accounts" in explain(signal)

    def test_classifies_throttling(self):
        assert "rate limit" in explain("429 Too Many Requests").lower()

    def test_classifies_network_faults(self):
        assert "did not complete" in explain("requests.exceptions.ConnectionError")

    def test_specific_rule_beats_the_generic_500_rule(self):
        """Ordering matters: a recognisable 500 must not fall through to a vaguer rule."""
        signal = "500 Server Error: Something went wrong"
        assert explain(signal) != explain("some unrecognisable garbage")

    def test_unknown_signal_admits_it_rather_than_guessing(self):
        """A confident wrong diagnosis outlives the incident; an honest gap does not."""
        out = explain("wharrgarbl")
        assert "Not a signature this log recognises yet" in out
        assert "recorded above verbatim" in out

    def test_empty_signal_does_not_crash(self):
        assert explain("") != ""


class TestRender:
    def test_a_clean_run_renders_nothing(self):
        assert render("abc123", "production", []) == ""

    def test_includes_all_four_fields(self):
        out = render("abc123", "production", [
            _incident(summary="S", reasoning="R", remedy="M", report="COGS"),
        ])
        assert "**What happened:** S" in out
        assert "**Why:** R" in out
        assert "**What the system did:** M" in out
        assert "COGS" in out

    def test_marks_resolved_incidents_as_needing_no_action(self):
        out = render("abc", "production", [_incident(kind="self_heal", resolved=True)])
        assert "Resolved automatically" in out

    def test_marks_unresolved_incidents_as_needing_an_operator(self):
        out = render("abc", "production", [_incident()])
        assert "Needs an operator" in out

    def test_header_counts_incidents_and_self_heals(self):
        out = render("abc", "production", [
            _incident(),
            _incident(kind="self_heal", resolved=True),
        ])
        assert "2 incidents, 1 self-healed" in out

    def test_singular_wording_for_one_incident(self):
        out = render("abc", "production", [_incident()])
        assert "1 incident" in out
        assert "1 incidents" not in out

    def test_includes_the_run_url_when_given(self):
        out = render("abc", "production", [_incident()], run_url="https://example.com/r/1")
        assert "https://example.com/r/1" in out

    def test_pipeline_scoped_incident_is_labelled_pipeline(self):
        assert "· pipeline" in render("abc", "production", [_incident(report=None)])


class TestFlush:
    def test_creates_the_file_with_a_header(self, tmp_path):
        path = tmp_path / "docs" / "incident-log.md"
        record(_incident())
        assert flush("abc", "production", path=str(path)) is True
        text = path.read_text()
        assert text.startswith("# Incident Log")
        assert "guardrail_hold" in text

    def test_appends_without_repeating_the_header(self, tmp_path):
        path = tmp_path / "log.md"
        record(_incident(summary="first"))
        flush("run1", "production", path=str(path))
        reset()
        record(_incident(summary="second"))
        flush("run2", "production", path=str(path))

        text = path.read_text()
        assert text.count("# Incident Log") == 1
        assert "first" in text and "second" in text
        assert text.index("first") < text.index("second")   # newest at the bottom

    def test_a_clean_run_writes_nothing_and_creates_no_file(self, tmp_path):
        path = tmp_path / "log.md"
        assert flush("abc", "production", path=str(path)) is False
        assert not path.exists()

    def test_never_raises_on_an_unwritable_path(self, tmp_path):
        """Failing to log must not fail the run."""
        blocker = tmp_path / "notadir"
        blocker.write_text("i am a file")
        record(_incident())
        assert flush("abc", "production", path=str(blocker / "log.md")) is False

    def test_omitting_the_path_writes_to_the_module_default(self, tmp_path, monkeypatch):
        """The default is LOG_PATH, which conftest redirects so tests can't touch
        the real log. Asserting the behaviour rather than the literal string keeps
        this honest under that redirect."""
        target = tmp_path / "default.md"
        monkeypatch.setattr(incidents, "LOG_PATH", str(target))
        record(_incident())
        assert flush("abc", "production") is True
        assert target.exists()
