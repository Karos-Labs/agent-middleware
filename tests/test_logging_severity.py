"""The startup alarm has to be readable by the thing that watches for alarms.

`main.py` logs an ERROR when `AUTH_ROLE_BINDINGS` is empty — the state where
authorization is off while looking on, and every verified caller holds admin.
It had been logging that in both environments since the role model shipped, and
it was found by reading the config rather than by anyone seeing it.

The cause was the log FORMAT, not the log. Cloud Run stores a line that is not
valid JSON as a `textPayload` with severity `DEFAULT`: the level is in the text
and nothing parses it, so the console greys it out with everything else, a
log-based metric cannot filter on `severity>=ERROR`, and an alert built on one
never fires.
"""

import json
import logging

import pytest

from app.logging_config import (
    CloudLoggingFormatter,
    cloud_run_environment,
    configure_logging,
)


def _record(level: int, message: str, name: str = "app.main") -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname="/app/main.py", lineno=203,
        msg=message, args=(), exc_info=None, func="lifespan",
    )


def test_error_carries_a_severity_cloud_logging_reads():
    line = CloudLoggingFormatter().format(_record(logging.ERROR, "AUTH_ROLE_BINDINGS is empty"))
    payload = json.loads(line)
    assert payload["severity"] == "ERROR"
    assert payload["message"] == "AUTH_ROLE_BINDINGS is empty"


def test_every_line_is_one_json_object():
    # Cloud Run parses per line. A formatter that pretty-printed would produce
    # one log entry per brace, which reads as corruption rather than as a bug.
    line = CloudLoggingFormatter().format(_record(logging.INFO, "started\nsecond line"))
    assert "\n" not in line
    assert json.loads(line)["message"] == "started\nsecond line"


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
    ],
)
def test_levels_map_onto_severities_cloud_logging_accepts(level, expected):
    assert json.loads(CloudLoggingFormatter().format(_record(level, "x")))["severity"] == expected


def test_the_python_only_aliases_are_translated():
    # `WARN` is a Python alias; Cloud Logging rejects it and the entry silently
    # drops to DEFAULT, which is the exact failure this file exists for.
    record = _record(logging.WARNING, "x")
    record.levelname = "WARN"
    assert json.loads(CloudLoggingFormatter().format(record))["severity"] == "WARNING"


def test_a_traceback_travels_inside_the_message():
    # Error Reporting groups on the stack trace in `message`. A traceback in a
    # sibling field is one nobody is ever shown.
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record(logging.ERROR, "failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(CloudLoggingFormatter().format(record))
    assert payload["message"].startswith("failed\n")
    assert "ValueError: boom" in payload["message"]


def test_an_unserialisable_value_is_rendered_rather_than_lost():
    # A log line is the last thing that should raise.
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    record = _record(logging.INFO, "value=%s")
    record.args = (Opaque(),)
    assert "<opaque>" in json.loads(CloudLoggingFormatter().format(record))["message"]


def test_cloud_run_is_detected_by_the_variable_only_cloud_run_sets():
    assert cloud_run_environment({"K_SERVICE": "agent-middleware"}) is True
    # A developer's machine holds gcloud credentials and a project id; neither
    # means the process is on Cloud Run, which is why K_SERVICE is the signal.
    assert cloud_run_environment({"GOOGLE_CLOUD_PROJECT": "karoscmo"}) is False
    assert cloud_run_environment({}) is False


def test_local_runs_keep_the_readable_format(capsys):
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    try:
        configure_logging("INFO", structured=False)
        logging.getLogger("app.test").info("hello")
        assert "{" not in capsys.readouterr().out
    finally:
        root.handlers = saved


def test_cloud_run_runs_emit_json(capsys):
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    try:
        configure_logging("INFO", structured=True)
        logging.getLogger("app.test").error("hello")
        assert json.loads(capsys.readouterr().out.strip())["severity"] == "ERROR"
    finally:
        root.handlers = saved
