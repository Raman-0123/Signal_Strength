import tempfile
import unittest
from pathlib import Path

from speedy_scraper.domain import JobStatus
from speedy_scraper.repository import _ALLOWED_TRANSITIONS, LeadRepository


class JobStateMachineTests(unittest.TestCase):
    def test_every_state_transition_branch_is_explicitly_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = LeadRepository(Path(directory) / "states.db")
            repository.migrate()

            def create_in_state(status: JobStatus):
                job = repository.create_job("reconcile", {"workflow": "reconcile"})
                if status == JobStatus.QUEUED:
                    return job
                if status == JobStatus.RUNNING:
                    return repository.transition(job.id, JobStatus.RUNNING)
                if status == JobStatus.WAITING_VERIFICATION:
                    repository.transition(job.id, JobStatus.RUNNING)
                    return repository.transition(job.id, status)
                if status in {JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED}:
                    repository.transition(job.id, JobStatus.RUNNING)
                    return repository.transition(job.id, status)
                if status in {JobStatus.FAILED, JobStatus.EXHAUSTED}:
                    repository.transition(job.id, JobStatus.RUNNING)
                    return repository.transition(job.id, status)
                return repository.transition(job.id, status)

            for source in JobStatus:
                for target in JobStatus:
                    with self.subTest(source=source, target=target):
                        job = create_in_state(source)
                        if target == source or target in _ALLOWED_TRANSITIONS[source]:
                            self.assertEqual(repository.transition(job.id, target).status, target)
                        else:
                            with self.assertRaisesRegex(ValueError, "Invalid job transition"):
                                repository.transition(job.id, target)


if __name__ == "__main__":
    unittest.main()
