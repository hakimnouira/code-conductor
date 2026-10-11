# Unified AI Coding Hub for VS Code

A Windows-first VS Code extension and local Python/FastAPI backend integrating multiple AI coding agents (OpenAI Codex CLI, GitHub Copilot SDK, Google Antigravity IDE, Ollama, and MCP servers) into a single unified chat and orchestration interface without requiring cloud API keys.

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
│   │   ├── models/         # Pydantic schemas (AgentInfo, TaskRequest, Events)
│   │   ├── adapters/       # Base adapter interface, Mock Echo adapter, Agent registry
│   │   └── main.py         # FastAPI application, SSE endpoints, task cancellation
│   ├── tests/              # Pytest suite (auth, health, agent discovery, tasks, SSE)
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

## Milestones Progress

- [x] **Milestone 1**: Research, architecture, and dependency compatibility.
- [x] **Milestone 2**: Minimal FastAPI backend (token auth, health check, SSE) and working VS Code chat extension.
- [x] **Milestone 3**: Codex CLI integration using existing ChatGPT subscription login.
- [ ] **Milestone 4**: GitHub Copilot SDK integration using existing subscription.
- [ ] **Milestone 5**: Antigravity MCP integration experiment.
- [ ] **Milestone 6**: Ollama integration and agent registry.
- [ ] **Milestone 7**: Safe workspace edits, Git diff inspection, and test runner.
- [ ] **Milestone 8**: Multi-agent planning, parallel workers, review, and task history.
- [ ] **Milestone 9**: Packaging, Windows integration tests, and documentation.
