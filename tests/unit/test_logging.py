import logging

import pytest

from app.core.logging import setup_logging


@pytest.fixture(autouse=True)
def restore_levels():
    loggers = [logging.getLogger(), logging.getLogger("sqlalchemy.engine")]
    levels = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


def test_sets_the_app_log_level():
    setup_logging("WARNING", sql_echo=False)

    assert logging.getLogger().level == logging.WARNING


def test_sql_is_not_logged_by_default():
    setup_logging("DEBUG", sql_echo=False)

    # Even at DEBUG, SQL statements (with message bodies) stay out of the log.
    assert not logging.getLogger("sqlalchemy.engine").isEnabledFor(logging.INFO)


def test_sql_echo_logs_statements():
    setup_logging("INFO", sql_echo=True)

    assert logging.getLogger("sqlalchemy.engine").isEnabledFor(logging.INFO)


def test_websocket_token_is_hidden_in_uvicorn_logs(caplog):
    setup_logging("INFO", sql_echo=False)
    uvicorn_logger = logging.getLogger("uvicorn.error")

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        # the same call uvicorn makes when it accepts a WebSocket
        uvicorn_logger.info(
            '%s - "WebSocket %s" [accepted]',
            "172.18.0.1:52344",
            "/api/chat/v1/ws?token=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln&v=1",
        )

    assert "eyJhbGci" not in caplog.text
    assert '"WebSocket /api/chat/v1/ws?token=***&v=1" [accepted]' in caplog.text
