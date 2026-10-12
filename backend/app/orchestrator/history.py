import json
from pathlib import Path
from app.orchestrator.models import OrchestrationAuditRecord

DEFAULT_HISTORY_FILE = Path.home() / ".aihub_history.jsonl"


class AuditHistoryTracker:
    """
    Append-only audit history log for all orchestration tasks, plans,
    worker outputs, and code review verdicts.
    """

    def __init__(self, history_file: Path | None = None):
        self.history_file = history_file or DEFAULT_HISTORY_FILE

    def append_record(self, record: OrchestrationAuditRecord) -> None:
        try:
            line = record.model_dump_json() + "\n"
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def list_records(self, limit: int = 50) -> list[OrchestrationAuditRecord]:
        if not self.history_file.exists():
            return []

        records: list[OrchestrationAuditRecord] = []
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = OrchestrationAuditRecord.model_validate_json(line)
                        records.append(record)
                    except Exception:
                        continue
        except Exception:
            return []

        # Return latest records first
        return records[-limit:][::-1]

    def get_record(self, task_id: str) -> OrchestrationAuditRecord | None:
        for record in self.list_records(limit=200):
            if record.task_id == task_id:
                return record
        return None

    def clear(self) -> None:
        if self.history_file.exists():
            try:
                self.history_file.unlink()
            except Exception:
                pass
