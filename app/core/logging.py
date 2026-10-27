import logging
import re

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_TOKEN = re.compile(r'token=[^ &"]+')


class RedactTokens(logging.Filter):
    """uvicorn logs the WebSocket URL with ?token=<JWT>. Hide it, so nobody can log in from logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()  # the text with the %s arguments filled in
        redacted = _TOKEN.sub("token=***", message)
        if redacted != message:
            record.msg = redacted
            record.args = None  # already filled in; formatting them again would fail
        return True  # keep the record, only change it


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
    logging.getLogger("uvicorn.error").addFilter(_REDACT_TOKENS)
