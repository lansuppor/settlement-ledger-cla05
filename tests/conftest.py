import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_db() -> None:
    """每个测试使用独立的临时 SQLite 文件，避免跨用例与跨模块数据串扰。"""
    os.environ["APP_DB"] = os.path.join(tempfile.mkdtemp(), "pytest.sqlite")
    from app.store.db import migrate

    migrate()
    yield
