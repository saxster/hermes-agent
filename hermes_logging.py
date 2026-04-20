"""Centralized logging setup for Hermes Agent.

Provides a single ``setup_logging()`` entry point that both the CLI and
gateway call early in their startup path.  All log files live under
``~/.hermes/logs/`` (profile-aware via ``get_hermes_home()``).

Log files produced:
    agent.log   — INFO+, all agent/tool/session activity (the main log)
    errors.log  — WARNING+, errors and warnings only (quick triage)

Both files use ``RotatingFileHandler`` with ``RedactingFormatter`` so
secrets are never written to disk.

F-M4 request correlation: a ``request_id`` contextvar is threaded through
the gateway entry points (``X-Hermes-Request-Id`` header, echoed back in
responses). Log records emitted inside a bound context get the id stamped
via ``RequestIdFilter``, so grepping the log file for the id returns the
full trace of one request across tools, subagents, compression, etc.
"""

import logging
import os
import uuid
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, Optional

from contextlib import contextmanager

from hermes_constants import get_hermes_home


# F-M4: request-id correlation. Contextvar so nested async tasks and
# threaded tool handlers inherit the active request id automatically.
_request_id: ContextVar[str] = ContextVar("hermes_request_id", default="")


def generate_request_id() -> str:
    """Return a short url-safe id for a new request. 12 hex chars is enough
    to disambiguate within a single log file; smaller than a full uuid4 so
    grep output stays scannable."""
    return uuid.uuid4().hex[:12]


def get_request_id() -> str:
    """Return the request id bound to the current context, or empty string."""
    return _request_id.get()


@contextmanager
def bind_request_id(request_id: Optional[str] = None) -> Iterator[str]:
    """Bind ``request_id`` for the duration of the context.

    If ``request_id`` is None/empty, a fresh id is generated. Yields the
    effective id so callers can echo it in response headers.
    """
    rid = request_id or generate_request_id()
    token = _request_id.set(rid)
    try:
        yield rid
    finally:
        _request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Inject the current ``request_id`` into every LogRecord.

    Records outside a bound context get an empty string, which formats as
    nothing when the format string uses ``%(request_id)s``.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.request_id = _request_id.get() or "-"
        return True

# Sentinel to track whether setup_logging() has already run.  The function
# is idempotent — calling it twice is safe but the second call is a no-op
# unless ``force=True``.
_logging_initialized = False

# Default log format — includes timestamp, level, logger name, and message.
# F-M4: include request_id so gateway traces can be reconstructed by grep.
# When no request is in context the filter stamps "-" (reads as visually
# absent without breaking fixed-width column layout).
_LOG_FORMAT = "%(asctime)s %(levelname)s [req=%(request_id)s] %(name)s: %(message)s"
_LOG_FORMAT_VERBOSE = "%(asctime)s [req=%(request_id)s] %(name)s %(levelname)s: %(message)s"

# Third-party loggers that are noisy at DEBUG/INFO level.
_NOISY_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
    "asyncio",
    "hpack",
    "hpack.hpack",
    "grpc",
    "modal",
    "urllib3",
    "urllib3.connectionpool",
    "websockets",
    "charset_normalizer",
    "markdown_it",
)


def setup_logging(
    *,
    hermes_home: Optional[Path] = None,
    log_level: Optional[str] = None,
    max_size_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
    mode: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Configure the Hermes logging subsystem.

    Safe to call multiple times — the second call is a no-op unless
    *force* is ``True``.

    Parameters
    ----------
    hermes_home
        Override for the Hermes home directory.  Falls back to
        ``get_hermes_home()`` (profile-aware).
    log_level
        Minimum level for the ``agent.log`` file handler.  Accepts any
        standard Python level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``).
        Defaults to ``"INFO"`` or the value from config.yaml ``logging.level``.
    max_size_mb
        Maximum size of each log file in megabytes before rotation.
        Defaults to 5 or the value from config.yaml ``logging.max_size_mb``.
    backup_count
        Number of rotated backup files to keep.
        Defaults to 3 or the value from config.yaml ``logging.backup_count``.
    mode
        Hint for the caller context: ``"cli"``, ``"gateway"``, ``"cron"``.
        Currently used only for log format tuning (gateway includes PID).
    force
        Re-run setup even if it has already been called.

    Returns
    -------
    Path
        The ``logs/`` directory where files are written.
    """
    global _logging_initialized
    if _logging_initialized and not force:
        home = hermes_home or get_hermes_home()
        return home / "logs"

    home = hermes_home or get_hermes_home()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Read config defaults (best-effort — config may not be loaded yet).
    cfg_level, cfg_max_size, cfg_backup = _read_logging_config()

    level_name = (log_level or cfg_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    max_bytes = (max_size_mb or cfg_max_size or 5) * 1024 * 1024
    backups = backup_count or cfg_backup or 3

    # Lazy import to avoid circular dependency at module load time.
    from agent.redact import RedactingFormatter

    root = logging.getLogger()

    # When force=True, drop any previously-installed RotatingFileHandlers
    # rooted in the target log_dir so the new configuration actually takes
    # effect. Without this, _add_rotating_handler's idempotent-by-path guard
    # skips replacement and the stale level/max_bytes/backup_count persists.
    if force:
        log_dir_resolved = log_dir.resolve()
        for existing in list(root.handlers):
            if not isinstance(existing, RotatingFileHandler):
                continue
            try:
                existing_path = Path(getattr(existing, "baseFilename", "")).resolve()
            except (OSError, ValueError):
                continue
            try:
                existing_path.relative_to(log_dir_resolved)
            except ValueError:
                continue  # handler points outside the target log dir
            root.removeHandler(existing)
            existing.close()

    # --- agent.log (INFO+) — the main activity log -------------------------
    _add_rotating_handler(
        root,
        log_dir / "agent.log",
        level=level,
        max_bytes=max_bytes,
        backup_count=backups,
        formatter=RedactingFormatter(_LOG_FORMAT),
    )

    # --- errors.log (WARNING+) — quick triage log --------------------------
    _add_rotating_handler(
        root,
        log_dir / "errors.log",
        level=logging.WARNING,
        max_bytes=2 * 1024 * 1024,
        backup_count=2,
        formatter=RedactingFormatter(_LOG_FORMAT),
    )

    # Ensure root logger level is low enough for the handlers to fire.
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    # Suppress noisy third-party loggers.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _logging_initialized = True
    return log_dir


def setup_verbose_logging() -> None:
    """Enable DEBUG-level console logging for ``--verbose`` / ``-v`` mode.

    Called by ``AIAgent.__init__()`` when ``verbose_logging=True``.
    """
    from agent.redact import RedactingFormatter

    root = logging.getLogger()

    # Avoid adding duplicate stream handlers.
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            if getattr(h, "_hermes_verbose", False):
                return

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT_VERBOSE, datefmt="%H:%M:%S"))
    handler._hermes_verbose = True  # type: ignore[attr-defined]
    handler.addFilter(RequestIdFilter())  # F-M4
    root.addHandler(handler)

    # Lower root logger level so DEBUG records reach all handlers.
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)

    # Keep third-party libraries at WARNING to reduce noise.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # rex-deploy at INFO for sandbox status.
    logging.getLogger("rex-deploy").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add_rotating_handler(
    logger: logging.Logger,
    path: Path,
    *,
    level: int,
    max_bytes: int,
    backup_count: int,
    formatter: logging.Formatter,
) -> None:
    """Add a ``RotatingFileHandler`` to *logger*, skipping if one already
    exists for the same resolved file path (idempotent).
    """
    resolved = path.resolve()
    for existing in logger.handlers:
        if (
            isinstance(existing, RotatingFileHandler)
            and Path(getattr(existing, "baseFilename", "")).resolve() == resolved
        ):
            return  # already attached

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count,
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    # F-M4: stamp request_id onto every record routed to this handler.
    handler.addFilter(RequestIdFilter())
    logger.addHandler(handler)


def _read_logging_config():
    """Best-effort read of ``logging.*`` from config.yaml.

    Returns ``(level, max_size_mb, backup_count)`` — any may be ``None``.
    """
    try:
        import yaml
        config_path = get_hermes_home() / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            log_cfg = cfg.get("logging", {})
            if isinstance(log_cfg, dict):
                return (
                    log_cfg.get("level"),
                    log_cfg.get("max_size_mb"),
                    log_cfg.get("backup_count"),
                )
    except Exception:
        pass
    return (None, None, None)
