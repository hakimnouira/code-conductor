# Unified AI Coding Hub for VS Code

A Windows-first VS Code extension and local Python/FastAPI backend integrating multiple AI coding agents (OpenAI Codex CLI, GitHub Copilot SDK, Google Antigravity IDE, Ollama, and MCP servers) into a single unified chat and orchestration interface without requiring cloud API keys.

---

## Standalone Web Command Center

A responsive local dashboard is available alongside the VS Code extension. It shows real backend health, agent availability, task events, Git status, an explicit orchestration plan preview, and a manual test-run action.

Quick start (Windows):
```powershell
cd web
npm install
npm run dev
```
Open http://127.0.0.1:5173 and configure the local backend URL and bearer token in **Connections & settings**. Start the backend first using the setup instructions below. See [web/README.md](web/README.md) for details. Keep the backend bound to localhost.

---

## Run Code Conductor from anywhere (Windows)

Install a user-wide `cxcode` command from the repository root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-cxcode.ps1
```

Then you can call it from any folder or terminal:

- `cxcode` or `cxcode start` — start the local dashboard.
- `cxcode stop` — stop the backend and dashboard.
- `cxcode test` — run backend tests and compile the VS Code extension.
- `cxcode setup` — install/repair Python, extension, and web dependencies.
- `cxcode where` — show which checkout the command launches.

The first setup can take a few minutes and requires Python 3.11+ and Node.js/npm. If Windows does not recognize `cxcode` in an already-open terminal, open a new terminal. The dashboard stays local at `http://127.0.0.1:5173`; choose the project/workspace you want to work on inside the dashboard.

---

## Architecture Overview

1. **VS Code Extension (TypeScript)**:
   - Webview chat view registered in the primary activity bar.
   - Dynamic agent selector (`Auto`, `Mock Echo`, and pluggable providers).
   - Real-time token streaming via Server-Sent Events (SSE).
   - In-flight task cancellation.
   - Session bearer token managed via the VS Code `SecretStorage` API.
2. **Local Backend (Python 3.11+ / FastAPI)**:
   - Binds strictly to `127.0.0.1` on port `8765`.
   - Ephemeral, cryptographically secure Bearer token authentication generated at launch.
   - Common asynchronous agent adapter architecture (`BaseAgentAdapter`).
   - Agent registry and task queue.
   - Native Windows process management and tree termination.

---

## Directory Structure

```text
├── backend/
│   ├── app/
│   │   ├── core/           # Configuration, security, bearer token auth
│   │   ├── models/         # Pydantic schemas (AgentInfo, TaskRequest, Safety, Git, Tests)
│   │   ├── adapters/       # Base adapter interface, Codex, Copilot, Antigravity, Ollama, Echo
│   │   ├── mcp/            # Generic MCP client and tool manager
│   │   ├── safety/         # WorkspaceSecurityManager, GitGuard, SafeTestRunner
│   │   └── main.py         # FastAPI application, SSE endpoints, task cancellation, safety APIs
│   ├── tests/              # Pytest suite (auth, health, agent discovery, tasks, safety, SSE)
│   └── requirements.txt    # Python dependencies
├── extension/
│   ├── src/
│   │   ├── backendClient.ts   # Native HTTP / SSE client with Bearer auth
│   │   ├── backendManager.ts  # Python process lifecycle manager on Windows
│   │   ├── chatViewProvider.ts# VS Code WebviewViewProvider for chat UI
│   │   └── extension.ts       # Extension entrypoint and commands
│   ├── media/                 # SVG icons and UI assets
│   ├── package.json           # VS Code extension manifest
│   └── tsconfig.json          # TypeScript compiler configuration
├── scripts/
│   ├── setup.ps1              # PowerShell environment setup script
│   ├── start.ps1              # Standalone backend launch script
│   ├── healthcheck.ps1        # Backend health verification script
│   └── test.ps1               # Automated test runner
├── pytest.ini
├── .gitignore
└── README.md
```

---

## Safe Workspace Operations & Git Guard

The backend implements strict safety controls for AI coding operations:
- **Canonical Path Resolution**: Prevents path traversal outside the workspace root (`pathlib.Path.resolve()`).
- **Sensitive File Protection**: Automatically blocks access to credentials, private keys (`.env*`, `*.pem`, `id_rsa*`, `*api_keys*.json`, `.git/`).
- **Atomic File Edits**: Writes new file contents to temporary files and executes atomic replace (`os.replace`) with unified diff calculation (`difflib`).
- **Git Guard**: Tracks branch name, modified, untracked, and deleted files (`git status --porcelain -b`), generates clean unified diffs (`git diff`), and creates safety checkpoints (`git stash create` and safety reference branches).
- **Safe Test Runner**: Executes allowlisted test runners (`pytest`, `npm test`, `python`) without shell injection, enforcing timeouts and clean Windows process tree termination (`taskkill /F /T /PID`).


---

## Getting Started on Windows

### Prerequisites
* Windows 10/11 x64
* PowerShell 5.1 or PowerShell Core
* Node.js v20+ (`node` and `npm` in PATH)
* Python 3.11+ (or `uv` package manager)

### 1. Setup Environment
Run the automated PowerShell setup script:
```powershell
.\scripts\setup.ps1
```

### 2. Run Test Suite
Verify backend authentication, endpoints, SSE streaming, and TypeScript compilation:
```powershell
.\scripts\test.ps1
```

### 3. Launching Standalone Backend (Optional for Testing)
```powershell
.\scripts\start.ps1
```
In a separate terminal, test health:
```powershell
.\scripts\healthcheck.ps1
```

---
---

## Multi-Agent Orchestration & Code Review Gatekeeper (Milestone 8)

The backend provides a full DAG-based multi-agent orchestration engine:
- **Dependency DAG Decomposition**: Breaks down broad user prompts into granular, single-responsibility subtasks (`PlanDecomposer`). Subtasks with independent file targets are grouped into disjoint topological batches (`compute_parallel_batches`).
- **Parallel Worker Dispatch**: Dispatches disjoint subtasks concurrently via `asyncio.gather` across available agent adapters (`ParallelOrchestrator`).
- **Automated Code Review Gatekeeper**: Analyzes outputs and diffs against security standards (`CodeReviewer`), catching dangerous functions (`eval`, `os.system`, `subprocess.Popen(shell=True)`), syntax markers, and hardcoded credentials (`api_key = "..."`, `ghp_...`).
- **Immutable Audit History**: Records all execution plans, subtasks, worker outputs, review verdicts, and execution metrics to an append-only JSONL log (`AuditHistoryTracker` at `~/.aihub_history.jsonl`). Accessible via `GET /orchestrator/history` and in the VS Code sidebar via the **Audit Log** button.

---

## Production Packaging & Windows Integration Tests (Milestone 9)

### 1. Standalone Production Packaging
To package the complete standalone VS Code extension:
```powershell
.\scripts\package.ps1
```
This script cleans old builds, runs strict TypeScript type-checking, builds the production bundle via `@vscode/vsce`, and outputs `dist/unified-ai-hub-0.1.0.vsix` (verified ~16 KB, excluded test/source bloat via `.vscodeignore`, with valid 128x128 PNG icon and MIT license).

To install the built package into your local VS Code:
```powershell
code --install-extension .\dist\unified-ai-hub-0.1.0.vsix --force
```

### 2. Comprehensive Test Suite & E2E Validation
Run the full 52-test automated suite (including 7 Windows-native E2E lifecycle tests and TypeScript compilation):
```powershell
.\scripts\test.ps1
```

---

## Agent Configuration & Subscription Reuse

The hub is designed to leverage your **existing subscriptions without requiring paid cloud API keys**:

| Engine | Protocol / Adapter | Auth / Subscription Discovery | Diagnostic & Fallback |
| :--- | :--- | :--- | :--- |
| **OpenAI Codex CLI** | Subprocess CLI (`codex exec -C <ws>`) | `~/.codex` session credentials (reusing ChatGPT Plus/Team/Enterprise). Auto-discovers bundled `codex.exe` in VS Code extension directory. | Non-interactive execution, graceful tree kill via `taskkill /F /T /PID`. |
| **GitHub Copilot** | Official `github-copilot-sdk` | Auto-detects GitHub Copilot VS Code session tokens or `gh auth login`. | Reports `unauthenticated` with login guidance if session not active. |
| **Google Antigravity** | Local Session & API Router | Auto-discovers `~/.gemini/antigravity/bin/agentapi.bat` and queries AI Router with rotating keys. | Reports `DISABLED` diagnostic with clear activation instructions if offline. |
| **Ollama / Local** | HTTP REST (`/api/generate`) | Connects to `http://127.0.0.1:11434`. Runs completely offline without internet or tokens. | Probes connectivity; returns `offline` diagnostic with command to start daemon. |
| **Multi-Agent Orchestrator** | DAG Pipeline & Parallel Workers | Internal coordinator querying available local and cloud adapters in disjoint topological batches. | Automatic code review gatekeeper rejecting dangerous commands or hardcoded keys. |
| **Multi-Model Router** | Fast API Rotation | 7 rotating Groq keys, 3 APMix keys, 3 CodeCraft keys in `~/.gemini/config/api_keys.json`. | Automatic failover upon rate limits (HTTP 429). |

---

## REST & SSE API Reference

The local daemon listens on `http://127.0.0.1:8765` with `Authorization: Bearer <token>` (token stored in `~/.aihub_token`).

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Server status, version, uptime, workspace root, active task count. |
| `GET` | `/agents` | List all discovered agents with availability state and diagnostics. |
| `POST` | `/tasks` | Create an execution task (`prompt`, `agent_id`, `workspace_path`). |
| `GET` | `/tasks/{id}/events` | Server-Sent Events (SSE) token and status stream. |
| `POST` | `/tasks/{id}/cancel` | Immediately cancel in-flight process and child trees. |
| `POST` | `/orchestrator/plan` | Decompose prompt into dependency DAG and disjoint parallel batches. |
| `GET` | `/orchestrator/history` | List immutable audit records of all multi-agent runs and review verdicts. |
| `GET` | `/orchestrator/history/{id}` | Retrieve specific audit record details. |
| `POST` | `/workspace/file/read` | Safely read file with path traversal and secret blocking. |
| `POST` | `/workspace/file/write` | Atomically write file with unified diff calculation. |
| `GET` | `/workspace/files` | List workspace files safely, excluding sensitive patterns. |
| `GET` | `/workspace/git/status` | Git branch and working tree status. |
| `POST` | `/workspace/git/diff` | Calculate clean unified diff against HEAD. |
| `POST` | `/workspace/git/checkpoint` | Create safety checkpoint branch or stash. |
| `POST` | `/workspace/test/run` | Execute allowlisted test runner with process sandboxing. |
| `GET` | `/schedules` | List durable recurring workflow schedules. |
| `POST` | `/schedules` | Schedule a saved workflow with interval and retry policy. |
| `PUT` | `/schedules/{schedule_id}` | Pause or resume a schedule. |
| `GET` | `/schedules/{schedule_id}/runs` | Retrieve durable schedule run history. |
| `POST` | `/browser/automation-test` | Run Playwright-backed browser diagnostics against the configured CDP browser. |

---

## Troubleshooting Guide

### 1. Codex CLI shows `unauthenticated`
* **Cause**: Codex session credentials in `~/.codex` have expired.
* **Fix**: Run `codex auth login` in PowerShell and log in with your ChatGPT account.

### 2. Copilot shows `unauthenticated`
* **Cause**: No active GitHub Copilot token in environment or VS Code.
* **Fix**: Run `gh auth login` in PowerShell or sign into GitHub via VS Code.

### 3. Ollama shows `offline`
* **Cause**: Ollama daemon is not running on port 11434.
* **Fix**: Open PowerShell and run `ollama serve`. To pull a model: `ollama pull qwen2.5-coder`.

### 4. Backend reports `401 Unauthorized` in VS Code
* **Cause**: Token mismatch between extension and backend daemon.
* **Fix**: The extension automatically syncs with `~/.aihub_token`. Run `Restart Backend Server` from the VS Code command palette.

---

## Milestones Progress

- [x] **Milestone 1**: Research, architecture, and dependency compatibility.
- [x] **Milestone 2**: Minimal FastAPI backend (token auth, health check, SSE) and working VS Code chat extension.
- [x] **Milestone 3**: Codex CLI integration using existing ChatGPT subscription login.
- [x] **Milestone 4**: GitHub Copilot SDK integration using existing subscription.
- [x] **Milestone 5**: Antigravity MCP integration experiment.
- [x] **Milestone 6**: Ollama integration and agent registry.
- [x] **Milestone 7**: Safe workspace edits, Git diff inspection, and test runner.
- [x] **Milestone 8**: Multi-agent planning, parallel workers, review, and task history.
- [x] **Milestone 9**: Packaging, Windows integration tests, and documentation.

## Agent permissions, notifications, and custom providers

### Google Antigravity
Code Conductor uses Google's documented `agy` CLI headless interface instead of the undocumented IDE `agentapi` route. Install the official Antigravity CLI, sign in interactively once, and ensure `agy` is on `PATH`. If needed, set `ANTIGRAVITY_CLI` to the full executable path. See Google's [headless CLI documentation](https://www.antigravity.google/docs/cli/headless/).

### Permission modes
- **Safe**: uses Antigravity's CLI sandbox; Codex and Copilot retain their restrictive defaults.
- **Balanced**: uses provider-configured permission behavior; Codex uses `--full-auto` and Copilot allows shell requests.
- **God Mode**: requires a confirmation dialog and backend confirmation flag. Antigravity uses `--dangerously-skip-permissions`, Codex uses `--dangerously-bypass-approvals-and-sandbox`, and Copilot approves tool permission requests. This can execute arbitrary commands and modify files; use only with trusted prompts and a reviewed workspace. Other providers may not implement every mode.

### Notifications
The web dashboard can notify on completion and failure/possible action-needed events. Browser notification permission is optional. Sound presets (chime, bell, alert, silent) are stored in browser local storage and can be tested in Connections & settings.

### Adding API-compatible endpoints
In **Connections & settings**, enter a provider ID, display name, base URL, model ID, and one or more API keys (one key per line). The connector tries keys in order if a request fails. Remote endpoints must use HTTPS; HTTP is allowed only for loopback local inference servers such as LM Studio. API keys are stored via the operating system credential store using `keyring`; provider metadata contains no secret values.

The generic connector supports OpenAI-compatible `/chat/completions` endpoints for text responses only. It does **not** provide file editing, shell tools, or subscription sign-in. For coding-agent capabilities, use a provider's official CLI/SDK and its supported authentication flow. Do not paste passwords, cookies, OAuth refresh tokens, or subscription session data into the API-key field.


## Durable Scheduled Workflows & Browser Verification

### Recurring workflow scheduler
Saved workflows can be scheduled from the dashboard's workflow library. Schedules are stored in the local SQLite task database, survive backend restarts, and support:
- Intervals from 15 minutes to 30 days, pause/resume, and deletion.
- Bounded retries (0–3) for failed task dispatches or failed task runs.
- Durable run records with status, task ID, attempt count, timestamps, and error details.
- Startup recovery of queued/running schedule records. Tasks interrupted by a backend restart are marked failed by the task store and can be retried by the scheduler.
- Dashboard run history, opening the underlying task timeline, and opt-in completion/failure notifications while the dashboard is open.

The backend must remain running for a scheduled job to fire. This first scheduler uses intervals rather than cron/time-zone expressions; after downtime it skips missed intervals and schedules the next occurrence from recovery time instead of launching a burst of stale jobs. Schedules execute the saved prompt in **Safe** permission mode and keep a snapshot of the workflow settings, so removing or editing the original preset does not silently rewrite an existing schedule.

API endpoints (Bearer token required):
- `GET /schedules` — list schedules.
- `POST /schedules` — create a recurring schedule from `workflow_id`, `interval_minutes`, optional `workspace_path`, `max_retries`, and `enabled`.
- `PUT /schedules/{schedule_id}` — pause/resume.
- `DELETE /schedules/{schedule_id}` — remove a schedule (historical run records are retained).
- `GET /schedules/{schedule_id}/runs` — inspect recent runs.

### Playwright-backed browser verification
When a task has `browser_verify_url` configured, Code Conductor attaches Playwright to the configured Chromium DevTools Protocol endpoint (default `http://127.0.0.1:9222`) and opens a separate tab. It captures:
- Page load state and main-document HTTP status.
- Uncaught page exceptions and browser console errors.
- HTTP error responses and failed requests as diagnostic evidence.
- A screenshot saved under `.code-conductor/artifacts/{task_id}/browser-verification.png`.
- A bounded text excerpt and structured checks added to the task timeline.

The verifier waits for DOM content and makes a bounded attempt to wait for network idle; applications with polling, streaming, or WebSockets may never become idle. Failed subresource requests are reported but do not alone fail a run. JavaScript errors, common visible error markers, screenshot capture failures, and a failed main document can fail verification and feed diagnostics into the existing bounded repair loop.

The backend installs the Playwright Python package through `backend/requirements.txt`. It does **not** download a browser binary; configure a local Chromium/Chrome instance with remote debugging enabled and connect it in the Browser Session panel. The additional endpoint is `POST /browser/automation-test` with `{"url":"http://127.0.0.1:5173","wait_seconds":2}`.

This provides stronger runtime evidence than a page-content smoke check, but it is not a substitute for explicit acceptance assertions, a full E2E suite, accessibility checks, or a security audit. Treat the browser report as evidence, not proof of correctness.

## Chat-first workspace and GitHub repository analysis

The standalone web command center uses a chat-first workspace with a persistent agent rail, live task responses, a reviewable plan panel, and contextual project status. Larger prompts can be decomposed into visible steps before execution; planned steps run sequentially with recent results passed forward, and execution stops when a step reports an error.

Use **GitHub repo** in the composer to shallow-clone a public repository into the selected workspace parent. The importer accepts only HTTPS `github.com` repository URLs, validates the destination folder, and never runs project code, installs dependencies, executes build scripts, or invokes repository hooks. After cloning, the composer is prefilled with a reverse-engineering prompt that asks the agent to map architecture, entry points, data flow, tests, and risks with file references.

The repository importer requires Git to be installed and available on `PATH`. Private repositories and non-GitHub hosts are not supported by this initial implementation. Review untrusted repository contents before asking an agent to modify them, and keep permission mode set to **Safe**.


## Automatic GitHub Research & Reuse Engine

Code Conductor evaluates each task before dispatch and, by default, searches public GitHub repositories when the task looks like implementation, UI/design, integration, debugging, testing, or architecture work. Clearly trivial edits and explanations are skipped to avoid needless latency and API use. The composer has a **Research ON/OFF** switch; it is on by default.

The local backend calls the public GitHub repository search API, ranks candidates using task relevance, community adoption, freshness, and license metadata, and shows the repository links and scores in the chat. When a candidate has a recognized permissive SPDX license (MIT, Apache-2.0, BSD-2-Clause/3-Clause, ISC, 0BSD, or Unlicense) and is no larger than 50 MB, Code Conductor may shallow-clone the top suitable candidate to `.code-conductor/references/<owner>__<repo>` within the selected workspace. These reference checkouts are excluded from the workspace Git status where possible. The agent receives links, license details, ranking reasons, and the local path so it can inspect relevant files and adapt patterns to the current project.

**Safety rules:** reference repositories are never executed, installed, built, or initialized with submodules by the research engine. No code is copied automatically. The agent is instructed to preserve the target project's stack, review the source license, keep required attribution, and adapt only relevant patterns. Unlicensed repositories may appear as research results but are not automatically cloned. A failed or rate-limited GitHub search does not block the user's task.

The research preview endpoint is `POST /research/github` with `prompt`, optional `workspace_path`, and optional `clone_best`. The task endpoint also accepts `github_research: true|false`. Anonymous GitHub API limits apply unless `GITHUB_TOKEN` or `GH_TOKEN` is set in the backend environment.


## Local authentication token

The backend creates or reuses the per-user token in `%USERPROFILE%\.aihub_token` on Windows (or `~/.aihub_token` elsewhere). The Windows launcher passes that token to Vite as `VITE_AI_HUB_TOKEN` at startup, so the UI and backend use the same generated token without embedding a shared secret in source control. The UI keeps the token in session storage, migrates/removes any legacy local-storage copy, and never commits or writes provider API keys to the frontend.


## Portable local paths

The web-model configuration is stored in the checkout's `.web_models.json` by default, so moving or cloning the repository does not require editing a drive-specific path. Set `CODE_CONDUCTOR_WEB_MODELS_PATH` to override it. The Windows launcher resolves the project directory relative to its own location; set `CODE_CONDUCTOR_CLOAK_EXE` to use a non-default CloakBrowser executable.


## Evidence-based browser verification

Tasks and saved workflows can include optional `browser_assertions` alongside `browser_verify_url`. Supported checks are:

- `expected_text`: every phrase must appear in the visible page text.
- `forbidden_text`: none of these phrases may appear.
- `required_selectors`: every CSS selector must match a visible element.
- `expected_url_contains` and `expected_title_contains`: verify the resulting route and document title.

Example task payload fragment:

```json
{
  "browser_verify_url": "http://localhost:3000",
  "browser_assertions": {
    "expected_text": ["Dashboard", "Signed in"],
    "forbidden_text": ["Application error"],
    "required_selectors": ["main", "[data-testid='app-ready']"],
    "expected_url_contains": "/dashboard",
    "expected_title_contains": "My App"
  }
}
```

Browser verification attaches to the configured local Chromium/CDP session and records runtime exceptions, console errors, the main-document HTTP status, failed requests, explicit assertion results, and a screenshot artifact. Failed subresource requests are retained as evidence but do not automatically fail a run. These checks improve confidence; they are not a proof of correctness or security.

The recurring scheduler persists schedules and run records in SQLite, recovers queued/running runs after a backend restart, retries failed dispatches or tasks according to the configured retry count, prevents overlapping executions of the same schedule, and exposes run history through `GET /schedules/{schedule_id}/runs`. The local backend must be running for schedules to execute.
