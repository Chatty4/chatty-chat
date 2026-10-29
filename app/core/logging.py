import logging
import re

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_TOKEN = re.compile(r'token=[^ &"]+')


class RedactTokens(logging.Filter):
    """uvicorn logs URLs with ?token=<JWT>. Hide it, so nobody can log in from logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        # One argument at a time: uvicorn's access formatter unpacks record.args.
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(arg) for arg in record.args)
        record.msg = _redact(record.msg)
        return True  # keep the record, only change it


def _redact(value: object) -> object:
    return _TOKEN.sub("token=***", value) if isinstance(value, str) else value


_REDACT_TOKENS = RedactTokens()


def setup_logging(level: str, sql_echo: bool) -> None:
    """Send the app's logs to stderr in one format. uvicorn only configures its own loggers, so
    without this, app logs below WARNING are dropped and the rest are printed without a format."""
    # No-op when the root logger already has handlers (pytest's, or a second create_app()).
    logging.basicConfig(format=FORMAT)
    logging.getLogger().setLevel(level)
    # Same output as create_async_engine(echo=True), minus its extra handler (lines printed twice).
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if sql_echo else logging.WARNING)
    # addFilter skips a filter that is already there, so a second create_app() adds nothing.
    for name in ("uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addFilter(_REDACT_TOKENS)
