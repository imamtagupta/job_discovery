from abc import ABC, abstractmethod

from .models import Job


class Notifier(ABC):
    @abstractmethod
    def notify(self, jobs: list[Job]) -> None: ...


class ConsoleNotifier(Notifier):
    def notify(self, jobs: list[Job]) -> None:
        if jobs:
            print(f"{len(jobs)} new high-quality match(es):")
            for job in jobs:
                print(f"- {job.title} @ {job.company_id}: {job.apply_url}")
