import logging

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(level: str, sql_echo: bool) -> None:
    """Send the app's logs to stderr in one format. uvicorn only configures its own loggers, so
    without this, app logs below WARNING are dropped and the rest are printed without a format."""
    # No-op when the root logger already has handlers (pytest's, or a second create_app()).
    logging.basicConfig(format=FORMAT)
    logging.getLogger().setLevel(level)
    # Same output as create_async_engine(echo=True), minus its extra handler (lines printed twice).
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if sql_echo else logging.WARNING)
