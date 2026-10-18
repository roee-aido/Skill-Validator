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


DEMO_ZIP = PROJECT_ROOT / "demo" / "human-writing-skill.zip"


@pytest.fixture
def svc(tmp_path, agent_cfg, system_cfg):
    from skill_validator.orchestrator import Services, make_brain

    engine = make_engine(tmp_path / "svc" / "app.db")
    services = Services(
        db_factory=make_session_factory(engine),
        storage=Storage(tmp_path / "svc"),
        agent_cfg=agent_cfg,
        system_cfg=system_cfg,
        brain_factory=make_brain,
    )
    yield services
    engine.dispose()


@pytest.fixture
def svc_db(svc):
    s = svc.db_factory()
    yield s
    s.close()


@pytest.fixture
def svc_user(svc_db):
    u = User(email="owner@example.test")
    svc_db.add(u)
    svc_db.commit()
    return u


@pytest.fixture
def demo_review(db, storage, user, system_cfg):
    """A review with the demo skill already in the source area (intake only, no tasks)."""
    from skill_validator.db import FileRecord, ReviewSession
    from skill_validator.intake import collect_upload, write_source

    res = collect_upload([(DEMO_ZIP.name, DEMO_ZIP.read_bytes())], system_cfg.intake, system_cfg.supported_formats)
    review = ReviewSession(user_id=user.id, skill_name=res.skill_name, source_hash=res.source_hash)
    db.add(review)
    db.flush()
    write_source(storage, user.id, review.id, res)
    for f in res.files:
        db.add(
            FileRecord(
                session_id=review.id, area="source", relative_path=f.path, type=f.type,
                size=f.size, hash=f.hash, supported=f.supported,
            )
        )
    db.commit()
    return review
