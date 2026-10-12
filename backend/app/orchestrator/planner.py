import json
import re
import uuid
from pathlib import Path

from app.orchestrator.models import ExecutionPlan, Subtask, SubtaskStatus


class PlanDecomposer:
    """
    Decomposes user coding goals into a dependency DAG of disjoint subtasks
    and groups them into safe parallel execution batches.
    """

    def __init__(self, workspace_root: str | Path | None = None):
        self.workspace_root = Path(workspace_root) if workspace_root else None

    def compute_parallel_batches(self, subtasks: list[Subtask]) -> list[list[str]]:
        """
        Computes topological execution batches.
        Subtasks within each batch have all prerequisites fulfilled and have
        mutually disjoint target_files, allowing safe parallel execution.
        """
        task_map = {t.id: t for t in subtasks}
        completed: set[str] = set()
        remaining = set(task_map.keys())
        batches: list[list[str]] = []

        while remaining:
            # Find all tasks whose dependencies are fully completed
            ready = [
                tid for tid in remaining
                if all(dep in completed for dep in task_map[tid].dependencies)
            ]

            if not ready:
                # Cycle detected or unresolved dependency; break cycle safely
                ready = list(remaining)[:1]

            # Partition ready tasks into disjoint batches (no overlapping target files)
            current_batch: list[str] = []
            claimed_files: set[str] = set()

            for tid in ready:
                target_set = set(task_map[tid].target_files)
                # If target files are disjoint from already claimed files in this batch
                if not target_set or target_set.isdisjoint(claimed_files):
                    current_batch.append(tid)
                    claimed_files.update(target_set)

            batches.append(current_batch)
            for tid in current_batch:
                completed.add(tid)
                remaining.remove(tid)

        return batches

    def heuristic_decompose(self, goal: str) -> ExecutionPlan:
        """
        Rule-based heuristic decomposition ensuring robust plans even when offline.
        """
        plan_id = f"plan-{uuid.uuid4().hex[:8]}"
        goal_lower = goal.lower()

        subtasks: list[Subtask] = []

        if any(w in goal_lower for w in ["refactor", "clean", "structure"]):
            s1 = Subtask(
                id="subtask-1",
                title="Analyze structure and inspect target files",
                prompt=f"Analyze repository files and determine refactoring scope for: {goal}",
                target_files=[],
                dependencies=[],
                assigned_agent="codex",
            )
            s2 = Subtask(
                id="subtask-2",
                title="Execute refactoring edits",
                prompt=f"Perform the code refactoring as requested: {goal}",
                target_files=["src/core.py"],
                dependencies=["subtask-1"],
                assigned_agent="codex",
            )
            s3 = Subtask(
                id="subtask-3",
                title="Update and verify unit tests",
                prompt="Update unit tests to reflect refactored logic and verify test suite passes.",
                target_files=["tests/test_core.py"],
                dependencies=["subtask-2"],
                assigned_agent="groq",
            )
            subtasks = [s1, s2, s3]

        elif any(w in goal_lower for w in ["test", "pytest", "spec"]):
            s1 = Subtask(
                id="subtask-1",
                title="Review target code and determine test cases",
                prompt=f"Identify functions, edge cases, and interfaces for testing: {goal}",
                target_files=[],
                dependencies=[],
                assigned_agent="groq",
            )
            s2 = Subtask(
                id="subtask-2",
                title="Implement test cases",
                prompt=f"Write comprehensive automated tests for: {goal}",
                target_files=["tests/test_feature.py"],
                dependencies=["subtask-1"],
                assigned_agent="codex",
            )
            subtasks = [s1, s2]

        else:
            # Default 3-stage plan: Interface/Schema -> Implementation -> Tests (disjoint files)
            s1 = Subtask(
                id="subtask-1",
                title="Define contracts, schemas, and interfaces",
                prompt=f"Define core data models and interfaces required for: {goal}",
                target_files=["app/models/schemas.py"],
                dependencies=[],
                assigned_agent="groq",
            )
            s2 = Subtask(
                id="subtask-2",
                title="Implement core functionality",
                prompt=f"Implement main logic adhering to contracts: {goal}",
                target_files=["app/services/core.py"],
                dependencies=["subtask-1"],
                assigned_agent="codex",
            )
            s3 = Subtask(
                id="subtask-3",
                title="Implement unit test suite",
                prompt=f"Implement comprehensive unit tests verifying: {goal}",
                target_files=["tests/test_service.py"],
                dependencies=["subtask-1"],
                assigned_agent="groq",
            )
            subtasks = [s1, s2, s3]

        batches = self.compute_parallel_batches(subtasks)
        return ExecutionPlan(
            plan_id=plan_id,
            goal=goal,
            subtasks=subtasks,
            parallel_batches=batches,
        )

    def decompose(self, goal: str, workspace_files: list[str] | None = None) -> ExecutionPlan:
        """
        Decomposes user goal into an ExecutionPlan.
        Attempts LLM-assisted decomposition via local config if available,
        falling back seamlessly to heuristic decomposition.
        """
        # Try LLM decomposition if Groq key exists in config
        from app.adapters.multimodel import load_api_config
        cfg = load_api_config()
        groq_keys = cfg.get("groq", {}).get("keys", [])

        if groq_keys:
            try:
                plan = self._llm_decompose(goal, cfg, groq_keys[0])
                if plan and plan.subtasks:
                    return plan
            except Exception:
                pass

        return self.heuristic_decompose(goal)

    def _llm_decompose(self, goal: str, config: dict, api_key: str) -> ExecutionPlan | None:
        import ssl
        import urllib.request

        prompt = (
            f"You are an expert AI software architect. Decompose the following coding goal into 2 to 4 structured subtasks:\n"
            f"Goal: '{goal}'\n\n"
            f"Return strictly valid JSON with this format:\n"
            f'{{"subtasks": [\n'
            f'  {{"id": "subtask-1", "title": "...", "prompt": "...", "target_files": ["file1.py"], "dependencies": [], "assigned_agent": "codex"}},\n'
            f'  {{"id": "subtask-2", "title": "...", "prompt": "...", "target_files": ["test1.py"], "dependencies": ["subtask-1"], "assigned_agent": "groq"}}\n'
            f"]}}\n"
            f"Ensure subtasks operating on independent files are listed without dependencies so they can run in parallel."
        )

        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = json.dumps({
            "model": "qwen/qwen3.8-27b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 1024,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()

        # Extract JSON from response
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            subtasks_data = parsed.get("subtasks", [])
            subtasks: list[Subtask] = []
            for item in subtasks_data:
                subtasks.append(Subtask(
                    id=item.get("id", f"subtask-{len(subtasks)+1}"),
                    title=item.get("title", "Subtask"),
                    prompt=item.get("prompt", ""),
                    target_files=item.get("target_files", []),
                    dependencies=item.get("dependencies", []),
                    assigned_agent=item.get("assigned_agent", "auto"),
                ))
            if subtasks:
                batches = self.compute_parallel_batches(subtasks)
                return ExecutionPlan(
                    plan_id=f"plan-{uuid.uuid4().hex[:8]}",
                    goal=goal,
                    subtasks=subtasks,
                    parallel_batches=batches,
                )
        return None
