"""Centralized logging configuration.

## Why there are two formatters

On Cloud Run, anything written to stdout that is NOT valid JSON is stored as a
`textPayload` with severity `DEFAULT`. The level is still in the text, and
nothing reads it: the console shows every line in the same grey, a log-based
metric cannot filter on `severity>=ERROR`, and an alert policy built on one
never fires.

That is not hypothetical here. `main.py` logs an ERROR at startup when
`AUTH_ROLE_BINDINGS` is empty — the state in which authorization is switched
off while looking switched on, and every verified caller holds admin. It has
been logging that in both environments since the role model shipped, and it was
found by reading the config, not by seeing the alarm.

So in Cloud Run this emits one JSON object per line with Cloud Logging's own
`severity` and `message` fields. Everywhere else it keeps the readable text
format, because a developer reading a terminal is the other half of the job.
"""

import json
import logging
import os
import sys

_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)

#: Python level name -> Cloud Logging severity. Every other name Python can
#: produce is already a valid severity, so the map holds only the exception:
#: `WARN` is a Python alias that Cloud Logging does not accept.
_SEVERITY_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL", "NOTSET": "DEFAULT"}


def cloud_run_environment(env: dict[str, str] | None = None) -> bool:
    """True when this process is running under Cloud Run.

    `K_SERVICE` is injected by the runtime and by nothing else, which is why it
    is the signal rather than, say, a project id that is equally present on a
    developer's machine holding gcloud credentials.
    """

    return bool((env if env is not None else os.environ).get("K_SERVICE"))


class CloudLoggingFormatter(logging.Formatter):
    """One JSON object per line, with the two fields Cloud Logging reads.

    `severity` and `message` are special-cased by the agent; everything else in
    the object is carried through as `jsonPayload`, so the source location is
    worth the few bytes when something is being chased.
    """

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname.upper()
        payload: dict[str, object] = {
            "severity": _SEVERITY_ALIASES.get(level, level),
            "message": record.getMessage(),
            "logger": record.name,
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": str(record.lineno),
                "function": record.funcName,
            },
        }
        # The traceback belongs in `message`: Error Reporting groups on it, and
        # a stack trace in a sibling field is one nobody is shown.
        if record.exc_info:
            payload["message"] = f"{payload['message']}\n{self.formatException(record.exc_info)}"
        # `default=str` rather than a failure: a log line is the last thing that
        # should raise, and an unserialisable extra is better rendered than lost.
        return json.dumps(payload, default=str)


def configure_logging(log_level: str = "INFO", *, structured: bool | None = None) -> None:
    """Configure root logging handlers exactly once.

    Safe to call multiple times (e.g. in tests) without duplicating handlers.

    `structured` defaults to "yes when running under Cloud Run"; pass it
    explicitly to pin either format, which is what the tests do.
    """

    root = logging.getLogger()
    if root.handlers:
        root.setLevel(log_level.upper())
        return

    use_json = cloud_run_environment() if structured is None else structured
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingFormatter() if use_json else logging.Formatter(_LOG_FORMAT))

    root.setLevel(log_level.upper())
    root.addHandler(handler)

    # Quiet down noisy third-party loggers by default.
    logging.getLogger("google.api_core").setLevel(logging.WARNING)
    logging.getLogger("google.auth").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
