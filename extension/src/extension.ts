import * as vscode from 'vscode';
import { BackendManager } from './backendManager';
import { ChatViewProvider } from './chatViewProvider';

let backendManager: BackendManager | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const outputChannel = vscode.window.createOutputChannel('AI Coding Hub');
  outputChannel.appendLine('[Extension] Activating Unified AI Coding Hub...');

  backendManager = new BackendManager(context, outputChannel);

  // Register Webview View Provider
  const chatProvider = new ChatViewProvider(context.extensionUri, backendManager.getClient());
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, chatProvider)
  );

  // Create a status bar item for instant one-click access
  const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBarItem.command = 'aiHub.openChat';
  statusBarItem.text = '$(robot) AI Hub';
  statusBarItem.tooltip = 'Open Unified AI Coding Hub Chat Panel';
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  // Register commands
  context.subscriptions.push(
    vscode.commands.registerCommand('aiHub.openChat', async () => {
      try {
        await vscode.commands.executeCommand('workbench.view.extension.ai-coding-hub');
      } catch (_) {}
      try {
        await vscode.commands.executeCommand('aiHub.chatView.focus');
      } catch (_) {}
    })
  );


  context.subscriptions.push(
    vscode.commands.registerCommand('aiHub.checkHealth', async () => {
      try {
        const health = await backendManager?.getClient().getHealth();
        vscode.window.showInformationMessage(
          `AI Hub Backend Online: v${health?.version} (Uptime: ${Math.round(health?.uptime_seconds ?? 0)}s)`
        );
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        vscode.window.showErrorMessage(`AI Hub Backend Offline: ${message}`);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('aiHub.restartBackend', async () => {
      vscode.window.showInformationMessage('Restarting AI Hub Backend...');
      backendManager?.stop();
      try {
        await backendManager?.start();
        vscode.window.showInformationMessage('AI Hub Backend restarted successfully.');
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        vscode.window.showErrorMessage(`Failed to start AI Hub Backend: ${message}`);
      }
    })
  );

  // Start backend process
  try {
    await backendManager.start();
    outputChannel.appendLine('[Extension] Backend started and verified.');
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    outputChannel.appendLine(`[Extension] Backend startup error: ${message}`);
    vscode.window.showWarningMessage(`AI Hub Backend startup: ${message}. You can retry with command "AI Hub: Restart Backend Server".`);
  }
}

export function deactivate(): void {
  if (backendManager) {
    backendManager.stop();
  }
}
