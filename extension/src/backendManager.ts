import * as child_process from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';
import { BackendClient } from './backendClient';

const TOKEN_SECRET_KEY = 'aiHub.sessionBearerToken';

export class BackendManager {
  private context: vscode.ExtensionContext;
  private process: child_process.ChildProcess | null = null;
  private client: BackendClient;
  private outputChannel: vscode.OutputChannel;
  private port = 8765;
  private pidFile: string;

  constructor(context: vscode.ExtensionContext, outputChannel: vscode.OutputChannel) {
    this.context = context;
    this.outputChannel = outputChannel;
    this.client = new BackendClient('127.0.0.1', this.port);
    this.pidFile = path.join(os.homedir(), '.aihub_backend.pid');
  }

  public getClient(): BackendClient {
    return this.client;
  }

  private resolveWorkspaceRoot(): string {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (workspaceFolders && workspaceFolders.length > 0) {
      for (const folder of workspaceFolders) {
        const candidate = folder.uri.fsPath;
        if (fs.existsSync(path.join(candidate, 'backend', 'app', 'main.py'))) {
          return candidate;
        }
      }
    }

    // Check sibling or parent directory relative to extension location
    const parentCandidate = path.resolve(this.context.extensionPath, '..');
    if (fs.existsSync(path.join(parentCandidate, 'backend', 'app', 'main.py'))) {
      return parentCandidate;
    }

    if (fs.existsSync(path.join(this.context.extensionPath, 'backend', 'app', 'main.py'))) {
      return this.context.extensionPath;
    }

    return (workspaceFolders && workspaceFolders.length > 0)
      ? workspaceFolders[0].uri.fsPath
      : this.context.extensionPath;
  }

  private resolvePythonExecutable(rootPath: string): string {
    const config = vscode.workspace.getConfiguration('aiHub');
    const customPath = config.get<string>('pythonPath', '');
    if (customPath && fs.existsSync(customPath)) {
      return customPath;
    }

    if (rootPath) {
      const venvWin = path.join(rootPath, '.venv', 'Scripts', 'python.exe');
      if (fs.existsSync(venvWin)) {
        return venvWin;
      }
      const venvPosix = path.join(rootPath, '.venv', 'bin', 'python');
      if (fs.existsSync(venvPosix)) {
        return venvPosix;
      }
    }

    return process.platform === 'win32' ? 'python.exe' : 'python3';
  }

  private isAiHubBackendProcess(pid: number): boolean {
    if (process.platform === 'win32') {
      try {
        const cmd = `(Get-CimInstance Win32_Process -Filter "ProcessId = ${pid}").CommandLine`;
        const output = child_process.execFileSync(
          'powershell.exe',
          ['-NoProfile', '-NonInteractive', '-Command', cmd],
          { encoding: 'utf8', timeout: 5000 }
        );
        if (!output) {
          return false;
        }
        const lower = output.toLowerCase();
        return lower.includes('uvicorn') && lower.includes('app.main:app');
      } catch (_) {
        return false;
      }
    } else {
      try {
        const procCmdPath = `/proc/${pid}/cmdline`;
        if (fs.existsSync(procCmdPath)) {
          const cmdline = fs.readFileSync(procCmdPath, 'utf8').toLowerCase();
          return cmdline.includes('uvicorn') && cmdline.includes('app.main:app');
        }
        const output = child_process.execFileSync(
          'ps',
          ['-p', pid.toString(), '-o', 'args='],
          { encoding: 'utf8', timeout: 4000 }
        );
        const lower = output.toLowerCase();
        return lower.includes('uvicorn') && lower.includes('app.main:app');
      } catch (_) {
        return false;
      }
    }
  }

  private cleanStaleTrackedProcess(): void {
    if (!fs.existsSync(this.pidFile)) {
      return;
    }

    try {
      const rawContent = fs.readFileSync(this.pidFile, 'utf8').trim();
      let pid = 0;

      // Handle JSON lock file format or legacy raw PID
      try {
        const parsed = JSON.parse(rawContent);
        if (parsed && typeof parsed.pid === 'number') {
          pid = parsed.pid;
        }
      } catch (_) {
        pid = parseInt(rawContent, 10);
      }

      if (pid && !isNaN(pid) && pid > 0) {
        this.outputChannel.appendLine(`[BackendManager] Checking recorded backend PID in lock file: ${pid}`);

        // Robust Windows-compatible process identity verification
        // Never terminate an unrelated process due to stale lock files or PID reuse
        const isVerifiedBackend = this.isAiHubBackendProcess(pid);
        if (!isVerifiedBackend) {
          this.outputChannel.appendLine(
            `[BackendManager] PID ${pid} is not a valid AI Hub backend process (PID reuse detected or process already exited). Skipping termination.`
          );
        } else {
          this.outputChannel.appendLine(
            `[BackendManager] Verified stale AI Hub backend process on PID ${pid}. Safely terminating.`
          );
          if (process.platform === 'win32') {
            try {
              child_process.execSync(`taskkill /F /T /PID ${pid} 2>nul`);
            } catch (_) {}
          } else {
            try {
              process.kill(pid, 'SIGKILL');
            } catch (_) {}
          }
        }
      }
    } catch (_) {} finally {
      try {
        if (fs.existsSync(this.pidFile)) {
          fs.unlinkSync(this.pidFile);
        }
      } catch (_) {}
    }
  }

  public async start(): Promise<void> {
    // 1. Synchronize token with ~/.aihub_token
    const tokenFile = path.join(os.homedir(), '.aihub_token');
    let token = '';
    if (fs.existsSync(tokenFile)) {
      try {
        token = fs.readFileSync(tokenFile, 'utf8').trim();
      } catch (_) {}
    }
    if (!token) {
      token = (await this.context.secrets.get(TOKEN_SECRET_KEY)) || '';
    }
    if (!token) {
      token = crypto.randomBytes(32).toString('hex');
    }
    try {
      fs.writeFileSync(tokenFile, token, 'utf8');
    } catch (_) {}
    await this.context.secrets.store(TOKEN_SECRET_KEY, token);
    this.client.setToken(token);

    // 2. Check if backend is already running and healthy with this token
    try {
      const health = await this.client.getHealth();
      this.outputChannel.appendLine(
        `[BackendManager] Connected to active FastAPI backend (v${health.version}) on 127.0.0.1:${this.port}`
      );
      return;
    } catch (_) {
      // Backend not running or token needs renewal
    }

    // 3. Clean up only our previous recorded backend PID if stale
    this.cleanStaleTrackedProcess();

    // 4. Resolve workspace and Python
    const rootPath = this.resolveWorkspaceRoot();
    const pythonPath = this.resolvePythonExecutable(rootPath);
    const backendPath = path.join(rootPath, 'backend');

    if (!fs.existsSync(path.join(backendPath, 'app', 'main.py'))) {
      throw new Error(`Cannot locate FastAPI backend application in: ${backendPath}`);
    }

    this.outputChannel.appendLine(`[BackendManager] Starting backend at ${backendPath}`);
    this.outputChannel.appendLine(`[BackendManager] Using Python: ${pythonPath}`);

    // Set environment variables for backend process
    const env = {
      ...process.env,
      AIHUB_AUTH_TOKEN: token,
      PYTHONPATH: backendPath,
    };

    // 5. Spawn Uvicorn process
    this.process = child_process.spawn(
      pythonPath,
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', this.port.toString()],
      {
        cwd: backendPath,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      }
    );

    if (this.process.pid) {
      try {
        const record = {
          pid: this.process.pid,
          port: this.port,
          startTime: Date.now(),
          workspaceRoot: rootPath,
          command: `${pythonPath} -m uvicorn app.main:app --host 127.0.0.1 --port ${this.port}`,
        };
        fs.writeFileSync(this.pidFile, JSON.stringify(record, null, 2), 'utf8');
      } catch (_) {}
    }

    this.process.stdout?.on('data', (data) => {
      this.outputChannel.append(`[FastAPI stdout] ${data.toString()}`);
    });

    this.process.stderr?.on('data', (data) => {
      this.outputChannel.append(`[FastAPI stderr] ${data.toString()}`);
    });

    this.process.on('exit', (code, signal) => {
      this.outputChannel.appendLine(`[BackendManager] Process exited with code ${code}, signal ${signal}`);
      const exitedPid = this.process?.pid;
      this.process = null;
      try {
        if (fs.existsSync(this.pidFile)) {
          const raw = fs.readFileSync(this.pidFile, 'utf8');
          if (!exitedPid || raw.includes(`"pid": ${exitedPid}`) || raw.includes(`"pid":${exitedPid}`) || raw.trim() === exitedPid.toString()) {
            fs.unlinkSync(this.pidFile);
          }
        }
      } catch (_) {}
    });

    // 6. Poll health endpoint until responsive
    const startTime = Date.now();
    const timeoutMs = 15000;
    while (Date.now() - startTime < timeoutMs) {
      try {
        await this.client.getHealth();
        this.outputChannel.appendLine(`[BackendManager] FastAPI backend is online and healthy on 127.0.0.1:${this.port}`);
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 500));
      }
    }

    throw new Error('Timed out waiting for FastAPI backend to start.');
  }

  public stop(): void {
    if (this.process && this.process.pid) {
      const pid = this.process.pid;
      this.outputChannel.appendLine(`[BackendManager] Terminating tracked backend child process: ${pid}`);
      if (process.platform === 'win32') {
        try {
          child_process.execSync(`taskkill /F /T /PID ${pid} 2>nul`);
        } catch (_) {}
      } else {
        try {
          process.kill(pid, 'SIGKILL');
        } catch (_) {}
      }
      this.process = null;

      try {
        if (fs.existsSync(this.pidFile)) {
          const raw = fs.readFileSync(this.pidFile, 'utf8');
          if (raw.includes(`"pid": ${pid}`) || raw.includes(`"pid":${pid}`) || raw.trim() === pid.toString()) {
            fs.unlinkSync(this.pidFile);
          }
        }
      } catch (_) {}
    }
  }
}
