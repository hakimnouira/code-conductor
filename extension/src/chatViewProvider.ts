import * as vscode from 'vscode';
import { BackendClient } from './backendClient';

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'aiHub.chatView';
  private view?: vscode.WebviewView;
  private backendClient: BackendClient;
  private activeStreamCancel?: () => void;
  private activeTaskId?: string;

  constructor(
    private readonly extensionUri: vscode.Uri,
    backendClient: BackendClient
  ) {
    this.backendClient = backendClient;
  }

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };

    webviewView.webview.html = this.getHtmlForWebview(webviewView.webview);

    webviewView.webview.onDidReceiveMessage(async (message) => {
      switch (message.type) {
        case 'refreshAgents':
          await this.loadAgents();
          break;
        case 'sendMessage':
          await this.handleSendMessage(message.prompt, message.agentId);
          break;
        case 'cancelTask':
          await this.handleCancelTask();
          break;
      }
    });

    // Initial load of agents
    this.loadAgents();
  }

  private async loadAgents(): Promise<void> {
    try {
      const agents = await this.backendClient.getAgents();
      this.postMessage({ type: 'setAgents', agents });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      this.postMessage({ type: 'setError', text: `Failed to load agents: ${message}` });
    }
  }

  private async handleSendMessage(prompt: string, agentId: string): Promise<void> {
    if (!prompt.trim()) {
      return;
    }

    try {
      this.postMessage({ type: 'taskStarted', prompt, agentId });
      const task = await this.backendClient.createTask(prompt, agentId);
      this.activeTaskId = task.task_id;

      const streamController = this.backendClient.streamEvents(
        task.task_id,
        (event) => {
          this.postMessage({ type: 'streamEvent', event });
        },
        () => {
          this.activeStreamCancel = undefined;
          this.activeTaskId = undefined;
          this.postMessage({ type: 'taskFinished' });
        },
        (err) => {
          this.activeStreamCancel = undefined;
          this.activeTaskId = undefined;
          this.postMessage({ type: 'taskError', error: err.message });
        }
      );

      this.activeStreamCancel = streamController.cancel;
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      this.postMessage({ type: 'taskError', error: message });
    }
  }

  private async handleCancelTask(): Promise<void> {
    if (this.activeTaskId) {
      try {
        await this.backendClient.cancelTask(this.activeTaskId);
      } catch {
        // Ignore cancel errors
      }
    }
    if (this.activeStreamCancel) {
      this.activeStreamCancel();
      this.activeStreamCancel = undefined;
    }
    this.activeTaskId = undefined;
    this.postMessage({ type: 'taskCancelled' });
  }

  private postMessage(message: unknown): void {
    this.view?.webview.postMessage(message);
  }

  private getHtmlForWebview(_webview: vscode.Webview): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI Coding Hub</title>
  <style>
    :root {
      --bg: var(--vscode-editor-background);
      --fg: var(--vscode-editor-foreground);
      --input-bg: var(--vscode-input-background);
      --input-fg: var(--vscode-input-foreground);
      --input-border: var(--vscode-input-border);
      --btn-bg: var(--vscode-button-background);
      --btn-fg: var(--vscode-button-foreground);
      --btn-hover: var(--vscode-button-hoverBackground);
      --border: var(--vscode-panel-border, #333);
      --badge-ready: #4caf50;
      --badge-disabled: #ff9800;
      --badge-error: #f44336;
    }
    body {
      margin: 0;
      padding: 12px;
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      color: var(--fg);
      background-color: var(--bg);
      display: flex;
      flex-direction: column;
      height: 100vh;
      box-sizing: border-box;
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 8px;
    }
    .agent-select {
      background: var(--input-bg);
      color: var(--input-fg);
      border: 1px solid var(--input-border);
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 12px;
      outline: none;
    }
    .status-badge {
      font-size: 11px;
      padding: 2px 6px;
      border-radius: 10px;
      background: var(--border);
      color: var(--fg);
    }
    .chat-history {
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding-right: 4px;
      margin-bottom: 8px;
    }
    .message {
      padding: 8px 12px;
      border-radius: 6px;
      line-height: 1.4;
      max-width: 95%;
      word-break: break-word;
    }
    .message.user {
      align-self: flex-end;
      background: var(--vscode-button-secondaryBackground, #2d3748);
      color: var(--vscode-button-secondaryForeground, #fff);
      border-bottom-right-radius: 1px;
    }
    .message.assistant {
      align-self: flex-start;
      background: var(--vscode-editorWidget-background, #1f2428);
      border: 1px solid var(--border);
      border-bottom-left-radius: 1px;
    }
    .message.system {
      align-self: center;
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      font-style: italic;
    }
    .input-container {
      display: flex;
      flex-direction: column;
      gap: 6px;
      border-top: 1px solid var(--border);
      padding-top: 8px;
    }
    textarea {
      width: 100%;
      height: 60px;
      background: var(--input-bg);
      color: var(--input-fg);
      border: 1px solid var(--input-border);
      border-radius: 4px;
      padding: 6px;
      resize: vertical;
      font-family: inherit;
      box-sizing: border-box;
      outline: none;
    }
    textarea:focus {
      border-color: var(--vscode-focusBorder);
    }
    .btn-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    button {
      background: var(--btn-bg);
      color: var(--btn-fg);
      border: none;
      padding: 6px 14px;
      border-radius: 4px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 500;
    }
    button:hover {
      background: var(--btn-hover);
    }
    button.cancel-btn {
      background: var(--badge-error);
      display: none;
    }
    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
  </style>
</head>
<body>
  <div class="header">
    <select id="agentSelect" class="agent-select">
      <option value="auto">Auto Router</option>
    </select>
    <span id="statusBadge" class="status-badge">Backend Ready</span>
  </div>

  <div id="chatHistory" class="chat-history">
    <div class="message system">Unified AI Coding Hub initialized. Select an agent and enter your request.</div>
  </div>

  <div class="input-container">
    <textarea id="promptInput" placeholder="Ask Codex, Copilot, Antigravity, or Local Model... (Ctrl+Enter to send)"></textarea>
    <div class="btn-row">
      <button id="cancelBtn" class="cancel-btn">Cancel Task</button>
      <div style="flex: 1;"></div>
      <button id="sendBtn">Send</button>
    </div>
  </div>

  <script>
    const vscode = acquireVsCodeApi();
    const chatHistory = document.getElementById('chatHistory');
    const promptInput = document.getElementById('promptInput');
    const sendBtn = document.getElementById('sendBtn');
    const cancelBtn = document.getElementById('cancelBtn');
    const agentSelect = document.getElementById('agentSelect');
    const statusBadge = document.getElementById('statusBadge');

    let currentAssistantMsgEl = null;

    function addMessage(role, text) {
      const el = document.createElement('div');
      el.className = 'message ' + role;
      el.textContent = text;
      chatHistory.appendChild(el);
      chatHistory.scrollTop = chatHistory.scrollHeight;
      return el;
    }

    sendBtn.addEventListener('click', () => {
      const text = promptInput.value.trim();
      if (!text) return;
      promptInput.value = '';
      sendBtn.disabled = true;
      cancelBtn.style.display = 'inline-block';
      addMessage('user', text);
      currentAssistantMsgEl = addMessage('assistant', '');
      vscode.postMessage({
        type: 'sendMessage',
        prompt: text,
        agentId: agentSelect.value
      });
    });

    cancelBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'cancelTask' });
    });

    promptInput.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        sendBtn.click();
      }
    });

    window.addEventListener('message', (event) => {
      const msg = event.data;
      switch (msg.type) {
        case 'setAgents':
          agentSelect.innerHTML = '<option value="auto">Auto Router</option>';
          msg.agents.forEach(a => {
            const opt = document.createElement('option');
            opt.value = a.id;
            opt.textContent = a.name + ' (' + a.state + ')';
            agentSelect.appendChild(opt);
          });
          break;
        case 'streamEvent':
          if (currentAssistantMsgEl) {
            if (msg.event.event_type === 'token') {
              currentAssistantMsgEl.textContent += msg.event.text;
            } else if (msg.event.event_type === 'started') {
              currentAssistantMsgEl.textContent = msg.event.text + '\\n';
            } else if (msg.event.event_type === 'error') {
              currentAssistantMsgEl.textContent += '\\n[Error] ' + msg.event.text;
            }
            chatHistory.scrollTop = chatHistory.scrollHeight;
          }
          break;
        case 'taskFinished':
        case 'taskCancelled':
        case 'taskError':
          sendBtn.disabled = false;
          cancelBtn.style.display = 'none';
          currentAssistantMsgEl = null;
          break;
        case 'setError':
          addMessage('system', msg.text);
          break;
      }
    });

    // Notify extension to refresh agent list
    vscode.postMessage({ type: 'refreshAgents' });
  </script>
</body>
</html>`;
  }
}
