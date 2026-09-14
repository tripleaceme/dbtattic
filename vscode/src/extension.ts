import * as vscode from 'vscode';
import * as cli from './cli';
import * as panel from './panel';
import { InsightsProvider, InvocationsProvider } from './tree';

const DEFAULT_SQL = `select invocation_id, generated_at, command, target,
       nodes_run, failures, node_seconds
  from v_run_history`;

let status: vscode.StatusBarItem;

async function refreshStatus(): Promise<void> {
  try {
    const i = await cli.info();
    if (i.stale) {
      status.text = '$(warning) dbtscope: rebuild needed';
      status.tooltip = i.message ?? 'Store schema is out of date.';
      status.command = 'dbtscope.rebuild';
    } else if (!i.exists) {
      status.text = '$(circle-outline) dbtscope: no store';
      status.tooltip = `No store yet at ${i.db_path}. Capture to create it.`;
      status.command = 'dbtscope.capture';
    } else {
      status.text = `$(database) dbtscope: ${i.invocations ?? 0}`;
      const dedup =
        i.node_versions && i.node_rows ? ` (${(i.node_rows / i.node_versions).toFixed(1)}x dedup)` : '';
      status.tooltip = new vscode.MarkdownString(
        [
          `**dbtscope**`,
          ``,
          `- store: \`${i.store}\` (from ${i.store_source})`,
          `- invocations: ${i.invocations}`,
          `- artifacts: ${i.artifacts_captured}`,
          `- nodes: ${i.node_versions} versions${dedup}`,
          `- capture: ${i.capture_when}`
        ].join('\n')
      );
      status.command = 'dbtscope.query';
    }
    status.show();
  } catch {
    // Not a dbt project, or the CLI is absent. Staying quiet is correct here --
    // the view's welcome content already explains how to install it.
    status.hide();
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const invocations = new InvocationsProvider();
  const insights = new InsightsProvider();

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('dbtscope.invocations', invocations),
    vscode.window.registerTreeDataProvider('dbtscope.insights', insights)
  );

  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  context.subscriptions.push(status);

  const refreshAll = async () => {
    invocations.refresh();
    insights.refresh();
    await refreshStatus();
  };

  context.subscriptions.push(
    vscode.commands.registerCommand('dbtscope.refresh', refreshAll),

    vscode.commands.registerCommand('dbtscope.query', async () => {
      await panel.show(context, 'Query', DEFAULT_SQL);
    }),

    vscode.commands.registerCommand('dbtscope.runInsight', async (title: string, sql: string) => {
      await panel.show(context, title, sql);
    }),

    vscode.commands.registerCommand('dbtscope.capture', async () => {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: 'dbtscope: capturing artifacts' },
        async () => {
          try {
            await cli.capture();
            await refreshAll();
          } catch (err) {
            vscode.window.showErrorMessage(
              `dbtscope capture failed: ${err instanceof Error ? err.message : String(err)}`
            );
          }
        }
      );
    }),

    vscode.commands.registerCommand('dbtscope.rebuild', async () => {
      const yes = await vscode.window.showWarningMessage(
        'Rebuild the dbtscope store from the archived JSON? The archive is the source of truth, so no captured history is lost.',
        { modal: true },
        'Rebuild'
      );
      if (yes !== 'Rebuild') {
        return;
      }
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'dbtscope: rebuilding store' },
        async () => {
          try {
            const out = await cli.rebuild();
            vscode.window.showInformationMessage(`dbtscope: ${out.trim()}`);
            await refreshAll();
          } catch (err) {
            vscode.window.showErrorMessage(
              `dbtscope rebuild failed: ${err instanceof Error ? err.message : String(err)}`
            );
          }
        }
      );
    }),

    vscode.commands.registerCommand('dbtscope.copyStatePath', async () => {
      try {
        const path = await cli.statePath(true);
        if (!path) {
          vscode.window.showWarningMessage('dbtscope: no successful invocation in the store yet.');
          return;
        }
        await vscode.env.clipboard.writeText(path);
        vscode.window.showInformationMessage(`dbtscope: copied --state path for the last successful run.`);
      } catch (err) {
        vscode.window.showErrorMessage(
          `dbtscope: ${err instanceof Error ? err.message : String(err)}`
        );
      }
    }),

    vscode.commands.registerCommand('dbtscope.openSettings', async () => {
      await vscode.commands.executeCommand('workbench.action.openSettings', 'dbtscope');
    })
  );

  // The store changes whenever dbt runs; watching it keeps the tree honest
  // without the user having to remember to refresh.
  const watcher = vscode.workspace.createFileSystemWatcher('**/.dbtscope/store.duckdb');
  context.subscriptions.push(
    watcher,
    watcher.onDidChange(refreshAll),
    watcher.onDidCreate(refreshAll)
  );

  void refreshAll();
}

export function deactivate(): void {
  // nothing to tear down: no daemon, no connection pool
}
