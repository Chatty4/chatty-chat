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
