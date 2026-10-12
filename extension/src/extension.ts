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

  const sendEditorPrompt = async (prompt: string): Promise<void> => {
    try {
      await vscode.commands.executeCommand('aiHub.openChat');
      await chatProvider.sendPrompt(prompt, 'auto');
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      vscode.window.showErrorMessage(`AI Hub editor action failed: ${message}`);
    }
  };

  const getEditorSelection = (): { editor: vscode.TextEditor; code: string; startLine: number; endLine: number } | undefined => {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showInformationMessage('Open a source file before using an AI editor action.');
      return undefined;
    }
    const selection = editor.selection;
    const selected = editor.document.getText(selection);
    const code = (selected.trim() ? selected : editor.document.lineAt(selection.active.line).text).slice(0, 12000);
    return {
      editor,
      code,
      startLine: selection.isEmpty ? selection.active.line + 1 : selection.start.line + 1,
      endLine: selection.isEmpty ? selection.active.line + 1 : selection.end.line + 1,
    };
  };

  context.subscriptions.push(
    vscode.commands.registerCommand('aiHub.explainSelection', async () => {
      const selected = getEditorSelection();
      if (!selected) return;
      await sendEditorPrompt(
        `Explain the following code clearly. Do not modify files. Identify its purpose, important logic, assumptions, and potential edge cases.\n\nFile: ${selected.editor.document.uri.fsPath}\nLines: ${selected.startLine}-${selected.endLine}\nLanguage: ${selected.editor.document.languageId}\n\n\`\`\`${selected.editor.document.languageId}\n${selected.code}\n\`\`\``
      );
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('aiHub.refactorSelection', async () => {
      const selected = getEditorSelection();
      if (!selected) return;
      await sendEditorPrompt(
        `Refactor the selected code in place while preserving behavior and public interfaces. Inspect surrounding code and project conventions first, avoid unrelated edits, and run relevant tests if available. Explain the changes and verification performed.\n\nFile: ${selected.editor.document.uri.fsPath}\nLines: ${selected.startLine}-${selected.endLine}\nLanguage: ${selected.editor.document.languageId}\n\n\`\`\`${selected.editor.document.languageId}\n${selected.code}\n\`\`\``
      );
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('aiHub.fixDiagnostics', async () => {
      const selected = getEditorSelection();
      if (!selected) return;
      const diagnostics = vscode.languages.getDiagnostics(selected.editor.document.uri)
        .filter(d => d.severity === vscode.DiagnosticSeverity.Error || d.severity === vscode.DiagnosticSeverity.Warning)
        .slice(0, 40)
        .map(d => `Line ${d.range.start.line + 1}, column ${d.range.start.character + 1}: ${d.severity === vscode.DiagnosticSeverity.Error ? 'Error' : 'Warning'} — ${d.message}`);
      if (!diagnostics.length) {
        vscode.window.showInformationMessage('No current errors or warnings were reported for this file.');
        return;
      }
      await sendEditorPrompt(
        `Fix the current VS Code diagnostics in this file. Inspect the actual file and related code before editing. Make the smallest correct changes, preserve behavior, and run relevant tests or type checks. Do not suppress diagnostics without fixing their cause.\n\nFile: ${selected.editor.document.uri.fsPath}\nLanguage: ${selected.editor.document.languageId}\nDiagnostics:\n${diagnostics.join('\n')}\n\nRelevant selected code:\n\`\`\`${selected.editor.document.languageId}\n${selected.code}\n\`\`\``
      );
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
