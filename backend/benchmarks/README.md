# Coding-agent benchmark suite

The suite provides a small reproducible baseline for comparing Code Conductor providers using the same prompts, fixtures, and executable acceptance tests.

## Run

1. Start the Code Conductor backend and sign in to the desired provider.
2. From the repository root, run:

   ```powershell
   python .\scripts\benchmark_tasks.py --workspace-root .\.code-conductor-benchmark-runs
   ```

3. To compare providers, run the same suite with an explicit provider override, for example `--agent codex`, `--agent copilot`, or `--agent antigravity`. Preserve each output directory so the runs can be compared.

Each case is copied to a fresh workspace. The harness records task status, verification status, duration, test-evidence count, evidence statuses, and the workspace path in `results.jsonl` and writes aggregate success rate and mean duration to `summary.json`. A case only passes if the task completes and its machine-readable verification status matches the suite expectation. Provider availability and model versions should be recorded alongside results when doing formal comparisons.

The current suite includes a Python discount calculation, Python interval merging, and JavaScript slugification. Each case has executable acceptance tests and runs in a fresh workspace. The suite is intentionally small; add more deterministic cases before using the metrics to rank providers. A successful run is evidence of passing the configured checks, not proof that code is secure or correct for every untested behavior.
