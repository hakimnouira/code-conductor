import * as child_process from 'child_process';
import * as crypto from 'crypto';
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
    // 1. Generate or retrieve session bearer token
    let token = await this.context.secrets.get(TOKEN_SECRET_KEY);
    if (!token) {
      token = crypto.randomBytes(32).toString('hex');
      await this.context.secrets.store(TOKEN_SECRET_KEY, token);
    }
    this.client.setToken(token);

    // 2. Locate workspace backend and Python virtual environment
    const workspaceFolders = vscode.workspace.workspaceFolders;
    const rootPath = workspaceFolders && workspaceFolders.length > 0
      ? workspaceFolders[0].uri.fsPath
      : path.resolve(__dirname, '..', '..');

    const pythonPath = path.join(rootPath, '.venv', 'Scripts', 'python.exe');
    const backendPath = path.join(rootPath, 'backend');

    this.outputChannel.appendLine(`[BackendManager] Starting backend at ${backendPath}`);
    this.outputChannel.appendLine(`[BackendManager] Using Python: ${pythonPath}`);

    // Set environment variables for backend process
    const env = {
      ...process.env,
      AIHUB_AUTH_TOKEN: token,
      PYTHONPATH: backendPath,
    };

    // 3. Spawn Uvicorn process
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

    // 4. Poll health endpoint until responsive
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
        } catch {
          // Process might already be stopped
        }
      } else {
        this.process.kill('SIGTERM');
      }
      this.process = null;
    }
  }
}
