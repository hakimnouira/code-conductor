import * as fs from 'fs';
import * as http from 'http';
import * as os from 'os';
import * as path from 'path';

export interface AgentInfo {
  id: string;
  name: string;
  description: string;
  state: 'ready' | 'missing_cli' | 'unauthenticated' | 'disabled' | 'quota_exhausted' | 'error';
  diagnostic: string;
  is_default: boolean;
}

export interface TaskResponse {
  task_id: string;
  status: string;
  agent_id: string;
  prompt: string;
  message: string;
}

export interface AgentEvent {
  task_id: string;
  event_type: 'started' | 'token' | 'tool_call' | 'tool_result' | 'log' | 'diff' | 'error' | 'completed' | 'cancelled';
  data: Record<string, unknown>;
  text: string;
  timestamp: number;
}

export class BackendClient {
  private host: string;
  private port: number;
  private token: string;

  constructor(host = '127.0.0.1', port = 8765, token = '') {
    this.host = host;
    this.port = port;
    this.token = token;
  }

  public setToken(token: string): void {
    this.token = token;
  }

  public getPort(): number {
    return this.port;
  }

  private reloadToken(): void {
    try {
      const tokenFile = path.join(os.homedir(), '.aihub_token');
      if (fs.existsSync(tokenFile)) {
        this.token = fs.readFileSync(tokenFile, 'utf8').trim();
      }
    } catch (_) {}
  }

  private request<T>(method: string, reqPath: string, body?: unknown, isRetry = false): Promise<T> {
    return new Promise((resolve, reject) => {
      if (!this.token) {
        this.reloadToken();
      }
      const dataString = body ? JSON.stringify(body) : undefined;
      const headers: http.OutgoingHttpHeaders = {
        'Authorization': `Bearer ${this.token}`,
      };
      if (dataString) {
        headers['Content-Type'] = 'application/json';
        headers['Content-Length'] = Buffer.byteLength(dataString);
      }

      const req = http.request(
        {
          host: this.host,
          port: this.port,
          path: reqPath,
          method,
          headers,
          timeout: 10000,
        },
        (res) => {
          let responseBody = '';
          res.on('data', (chunk) => {
            responseBody += chunk;
          });
          res.on('end', () => {
            if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
              try {
                const parsed = JSON.parse(responseBody) as T;
                resolve(parsed);
              } catch (e) {
                reject(new Error(`Failed to parse response: ${responseBody}`));
              }
            } else if (res.statusCode === 401 && !isRetry) {
              this.reloadToken();
              this.request<T>(method, reqPath, body, true).then(resolve).catch(reject);
            } else {
              reject(new Error(`HTTP ${res.statusCode}: ${responseBody}`));
            }
          });
        }
      );


      req.on('error', (err) => reject(err));
      req.on('timeout', () => {
        req.destroy();
        reject(new Error(`Request to ${path} timed out`));
      });

      if (dataString) {
        req.write(dataString);
      }
      req.end();
    });
  }

  public async getHealth(): Promise<{ status: string; version: string; uptime_seconds: number }> {
    return this.request('GET', '/health');
  }

  public async getAgents(): Promise<AgentInfo[]> {
    return this.request('GET', '/agents');
  }

  public async createTask(prompt: string, agentId = 'auto'): Promise<TaskResponse> {
    return this.request('POST', '/tasks', { prompt, agent_id: agentId });
  }

  public async cancelTask(taskId: string): Promise<{ task_id: string; cancelled: boolean; status: string }> {
    return this.request('POST', `/tasks/${taskId}/cancel`);
  }

  public async getOrchestrationPlan(prompt: string, workspacePath?: string): Promise<any> {
    return this.request('POST', '/orchestrator/plan', { prompt, workspace_path: workspacePath });
  }

  public async getOrchestrationHistory(limit = 20): Promise<any[]> {
    return this.request('GET', `/orchestrator/history?limit=${limit}`);
  }

  public streamEvents(
    taskId: string,
    onEvent: (event: AgentEvent) => void,
    onDone: () => void,
    onError: (err: Error) => void
  ): { cancel: () => void } {
    let aborted = false;
    if (!this.token) {
      this.reloadToken();
    }

    const req = http.request(

      {
        host: this.host,
        port: this.port,
        path: `/tasks/${taskId}/events`,
        method: 'GET',
        headers: {
          'Authorization': `Bearer ${this.token}`,
          'Accept': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
      },
      (res) => {
        let buffer = '';

        res.on('data', (chunk) => {
          if (aborted) {
            return;
          }
          buffer += chunk.toString();
          const lines = buffer.split('\n');
          buffer = lines.pop() ?? '';

          for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed.startsWith('data:')) {
              const dataStr = trimmed.replace(/^data:\s*/, '');
              if (dataStr === '[DONE]') {
                onDone();
                return;
              }
              try {
                const event = JSON.parse(dataStr) as AgentEvent;
                onEvent(event);
              } catch {
                // Ignore parse errors on raw keepalives
              }
            }
          }
        });

        res.on('end', () => {
          if (!aborted) {
            onDone();
          }
        });

        res.on('error', (err) => {
          if (!aborted) {
            onError(err);
          }
        });
      }
    );

    req.on('error', (err) => {
      if (!aborted) {
        onError(err);
      }
    });

    req.end();

    return {
      cancel: () => {
        aborted = true;
        req.destroy();
      },
    };
  }
}
