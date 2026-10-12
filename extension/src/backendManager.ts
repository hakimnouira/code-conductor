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

  private cleanStaleTrackedProcess(): void {
    if (!fs.existsSync(this.pidFile)) {
      return;
    }

    try {
      const pidStr = fs.readFileSync(this.pidFile, 'utf8').trim();
      const pid = parseInt(pidStr, 10);
      if (pid && !isNaN(pid)) {
        this.outputChannel.appendLine(`[BackendManager] Found previous backend PID in lock file: ${pid}`);
        if (process.platform === 'win32') {
          // Terminate only this specific PID and its tree, never blanket port termination
          try {
            child_process.execSync(`taskkill /F /T /PID ${pid} 2>nul`);
          } catch (_) {}
        } else {
          try {
            process.kill(pid, 'SIGKILL');
          } catch (_) {}
        }
      }
    } catch (_) {} finally {
      try {
        fs.unlinkSync(this.pidFile);
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
        fs.writeFileSync(this.pidFile, this.process.pid.toString(), 'utf8');
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
      this.process = null;
      try {
        if (fs.existsSync(this.pidFile)) {
          fs.unlinkSync(this.pidFile);
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
      this.outputChannel.appendLine(`[BackendManager] Terminating tracked backend PID: ${pid}`);
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
    }
    try {
      if (fs.existsSync(this.pidFile)) {
        fs.unlinkSync(this.pidFile);
      }
    } catch (_) {}
  }
}
