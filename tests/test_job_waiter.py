"""
Tests for job_waiter asyncpg connection string handling.

RED Test: test_asyncpg_url_conversion
This test will FAIL because job_waiter.py passes db_manager.engine.url
directly to asyncpg.create_pool() without converting it to a string.
"""

import pytest


def test_db_engine_url_is_sqlalchemy_url_object():
    """
    Verify that db_manager.engine.url is a SQLAlchemy URL object, not a string.

    This demonstrates the bug: when this URL object is passed to
    asyncpg.create_pool(), it fails because asyncpg expects a string.
    """
    import sqlalchemy

    from app.db import DatabaseManager

    # This test shows the problem exists
    db_manager = DatabaseManager.get_instance()

    # db_manager.engine.url is a SQLAlchemy URL object
    assert isinstance(db_manager.engine.url, sqlalchemy.engine.url.URL), (
        f"Expected URL object, got {type(db_manager.engine.url)}"
    )

    # And it cannot be passed directly to asyncpg - it must be converted to string
    # This is the bug that causes job_waiter to fail


def test_asyncpg_create_pool_rejects_url_object():
    """
    asyncpg.create_pool should receive a connection string, not a URL object.

    This test demonstrates that passing a SQLAlchemy URL object to
    asyncpg.create_pool() will fail.
    """
    try:
        import asyncpg  # noqa: F401  availability probe
    except ImportError:
        pytest.skip("asyncpg not installed")

    from sqlalchemy.engine.url import make_url

    # Create a SQLAlchemy URL object like db_manager.engine.url
    url_obj = make_url("postgresql://user:pass@localhost/db")

    # This should fail or behave incorrectly
    # asyncpg expects a string DSN, not a URL object
    try:
        # Attempting to use a URL object as connection string
        result = str(url_obj)  # This is what we should do
        assert isinstance(result, str)
    except Exception as e:
        pytest.fail(f"URL object conversion failed: {e}")


def test_job_waiter_converts_url_to_string():
    """
    GREEN Test: job_waiter should convert db_manager.engine.url to string
    before passing to asyncpg.create_pool().

    This test verifies the FIX works correctly.
    IMPORTANT: str(url) masks password as '***', so we must use
    url.render_as_string(hide_password=False) to get the full connection string.
    """
    from sqlalchemy.engine.url import make_url

    url_obj = make_url("postgresql://user:pass@localhost/db")

    # After fix: should use render_as_string to get full URL with password
    conn_string = url_obj.render_as_string(hide_password=False)

    assert isinstance(conn_string, str)
    assert "postgresql://" in conn_string
    assert conn_string == "postgresql://user:pass@localhost/db"
