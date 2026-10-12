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

  constructor(context: vscode.ExtensionContext, outputChannel: vscode.OutputChannel) {
    this.context = context;
    this.outputChannel = outputChannel;
    this.client = new BackendClient('127.0.0.1', this.port);
  }

  public getClient(): BackendClient {
    return this.client;
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

    // 3. Locate workspace backend and Python virtual environment
    let rootPath = 'c:\\Users\\hakim\\Desktop\\vs extention';
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (workspaceFolders && workspaceFolders.length > 0) {
      const candidate = workspaceFolders[0].uri.fsPath;
      if (fs.existsSync(path.join(candidate, 'backend', 'app', 'main.py'))) {
        rootPath = candidate;
      }
    }

    let pythonPath = path.join(rootPath, '.venv', 'Scripts', 'python.exe');
    if (!fs.existsSync(pythonPath)) {
      pythonPath = 'c:\\Users\\hakim\\Desktop\\vs extention\\.venv\\Scripts\\python.exe';
    }
    const backendPath = path.join(rootPath, 'backend');

    // If port 8765 is occupied by an unresponsive or mismatched process, kill it
    if (process.platform === 'win32') {
      try {
        child_process.execSync(
          `powershell -Command "Get-NetTCPConnection -LocalPort ${this.port} -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"`
        );
      } catch (_) {}
    }

    this.outputChannel.appendLine(`[BackendManager] Starting backend at ${backendPath}`);
    this.outputChannel.appendLine(`[BackendManager] Using Python: ${pythonPath}`);

    // Set environment variables for backend process
    const env = {
      ...process.env,
      AIHUB_AUTH_TOKEN: token,
      PYTHONPATH: backendPath,
    };

    // 4. Spawn Uvicorn process
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

    this.process.stdout?.on('data', (data) => {
      this.outputChannel.append(`[FastAPI stdout] ${data.toString()}`);
    });

    this.process.stderr?.on('data', (data) => {
      this.outputChannel.append(`[FastAPI stderr] ${data.toString()}`);
    });

    this.process.on('exit', (code, signal) => {
      this.outputChannel.appendLine(`[BackendManager] Process exited with code ${code}, signal ${signal}`);
      this.process = null;
    });

    // 5. Poll health endpoint until responsive
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
      this.outputChannel.appendLine(`[BackendManager] Terminating backend PID: ${this.process.pid}`);
      if (process.platform === 'win32') {
        try {
          child_process.execSync(`taskkill /F /T /PID ${this.process.pid}`);
        } catch (_) {}
      } else {
        this.process.kill();
      }
      this.process = null;
    }
  }
}
