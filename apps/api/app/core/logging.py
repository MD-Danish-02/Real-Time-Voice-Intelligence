"""
Structured JSON-ish logging. Never logs secrets: a fixed deny-list of keys
is scrubbed from every event before it is emitted.
"""
import logging
import sys

import structlog

_SENSITIVE_KEYS = {
    "password", "raw_password", "token", "access_token", "refresh_token",
    "jwt_secret", "api_key", "client_secret", "authorization", "secret",
    "encryption_key", "auth_token", "webhook_secret",
}


def _scrub(_, __, event_dict):
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(debug: bool = True) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG if debug else logging.INFO,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _scrub,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG if debug else logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "voxai"):
    return structlog.get_logger(name)
