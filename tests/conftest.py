"""pytest 공통 설정.

settings 를 임포트하기 전에 LOGIWISE_DB_PATH 를 임시 경로로 고정해, 테스트가 실제 data/logiwise.db 를 건드리지 않게 한다.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 1) settings 임포트보다 먼저 환경변수를 고정한다.
_SESSION_TMP = tempfile.mkdtemp(prefix="logiwise_test_")
os.environ["LOGIWISE_DB_PATH"] = os.path.join(_SESSION_TMP, "conftest.db")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 2) 프로젝트 루트를 import 경로에 넣는다.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from logiwise import seed, settings  # noqa: E402


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    """테스트마다 새 임시 DB 에 seed 한 경로. 환경변수도 같이 바꿔 path 를 생략한 호출도 이 DB 를 쓴다."""
    p = tmp_path / "logiwise_test.db"
    monkeypatch.setenv("LOGIWISE_DB_PATH", str(p))
    assert settings.get_db_path() == str(p)
    assert seed.seed(path=str(p)) is True
    return str(p)
