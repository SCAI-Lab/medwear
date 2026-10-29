"""
Shared pytest fixtures and hooks.
"""
import pytest


@pytest.fixture(autouse=True)
async def dispose_sqlalchemy_engines():
    """
    Dispose all cached SQLAlchemy async engines after each test.

    Prevents "Future attached to a different loop" errors when async tests
    run across different event loop scopes (e.g., session vs function scope).
    """
    yield
    try:
        import medwear_med_api.db as db_module
        for project_id in list(db_module._engines.keys()):
            await db_module.dispose_project_engine(project_id)
    except Exception:
        pass
