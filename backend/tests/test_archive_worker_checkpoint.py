"""JSON checkpoint writes must preserve lease fencing and finish successful jobs."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import ArchiveJob
from app.services.archive_worker import ArchiveWorker


def test_worker_json_checkpoint_completes_only_owned_job():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    worker = ArchiveWorker(session_factory=sessions)
    with sessions() as db:
        job = ArchiveJob(job_type='sync', status='running', lease_owner=worker.worker_id,
                         lease_expires_at='2099-01-01T00:00:00Z')
        other = ArchiveJob(job_type='sync', status='running', lease_owner='other-worker',
                           lease_expires_at='2099-01-01T00:00:00Z')
        db.add_all([job, other]); db.commit()
        job_id, other_id = job.id, other.id
    assert worker._finish_job(job_id, status='completed', cursor={'completed': True}, statistics={'attempts': 1})
    assert not worker._finish_job(other_id, status='completed', cursor={'completed': True})
    with sessions() as db:
        saved = db.get(ArchiveJob, job_id)
        assert saved.status == 'completed'
        assert saved.cursor == {'completed': True}
        assert saved.statistics == {'attempts': 1}
        assert saved.lease_owner is None and saved.completed_at
        assert db.get(ArchiveJob, other_id).status == 'running'
    engine.dispose()
