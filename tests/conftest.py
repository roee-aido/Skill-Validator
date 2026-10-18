import pytest

from skill_validator import PROJECT_ROOT
from skill_validator.config import load_agent_config, load_system_config
from skill_validator.db import User, make_engine, make_session_factory
from skill_validator.storage import Storage


@pytest.fixture
def agent_cfg():
    return load_agent_config(PROJECT_ROOT / "config" / "agent.mock.yaml")


@pytest.fixture
def system_cfg():
    return load_system_config()


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "data")


@pytest.fixture
def db(tmp_path):
    engine = make_engine(tmp_path / "data" / "app.db")
    factory = make_session_factory(engine)
    s = factory()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def user(db):
    u = User(email="demo@example.test")
    db.add(u)
    db.commit()
    return u
