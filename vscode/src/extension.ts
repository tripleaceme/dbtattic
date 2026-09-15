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
      status.text = '$(warning) dbtattic: rebuild needed';
      status.tooltip = i.message ?? 'Store schema is out of date.';
      status.command = 'dbtattic.rebuild';
    } else if (!i.exists) {
      status.text = '$(circle-outline) dbtattic: no store';
      status.tooltip = `No store yet at ${i.db_path}. Capture to create it.`;
      status.command = 'dbtattic.capture';
    } else {
      status.text = `$(database) dbtattic: ${i.invocations ?? 0}`;
      const dedup =
        i.node_versions && i.node_rows ? ` (${(i.node_rows / i.node_versions).toFixed(1)}x dedup)` : '';
      status.tooltip = new vscode.MarkdownString(
        [
          `**dbtattic**`,
          ``,
          `- store: \`${i.store}\` (from ${i.store_source})`,
          `- invocations: ${i.invocations}`,
          `- artifacts: ${i.artifacts_captured}`,
          `- nodes: ${i.node_versions} versions${dedup}`,
          `- capture: ${i.capture_when}`
        ].join('\n')
      );
      status.command = 'dbtattic.query';
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
    vscode.window.registerTreeDataProvider('dbtattic.invocations', invocations),
    vscode.window.registerTreeDataProvider('dbtattic.insights', insights)
  );

  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  context.subscriptions.push(status);

  const refreshAll = async () => {
    invocations.refresh();
    insights.refresh();
    await refreshStatus();
  };

  context.subscriptions.push(
    vscode.commands.registerCommand('dbtattic.refresh', refreshAll),

    vscode.commands.registerCommand('dbtattic.query', async () => {
      await panel.show(context, 'Query', DEFAULT_SQL);
    }),

    vscode.commands.registerCommand('dbtattic.runInsight', async (title: string, sql: string) => {
      await panel.show(context, title, sql);
    }),

    vscode.commands.registerCommand('dbtattic.capture', async () => {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: 'dbtattic: capturing artifacts' },
        async () => {
          try {
            await cli.capture();
            await refreshAll();
          } catch (err) {
            vscode.window.showErrorMessage(
              `dbtattic capture failed: ${err instanceof Error ? err.message : String(err)}`
            );
          }
        }
      );
    }),

    vscode.commands.registerCommand('dbtattic.rebuild', async () => {
      const yes = await vscode.window.showWarningMessage(
        'Rebuild the dbtattic store from the archived JSON? The archive is the source of truth, so no captured history is lost.',
        { modal: true },
        'Rebuild'
      );
      if (yes !== 'Rebuild') {
        return;
      }
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'dbtattic: rebuilding store' },
        async () => {
          try {
            const out = await cli.rebuild();
            vscode.window.showInformationMessage(`dbtattic: ${out.trim()}`);
            await refreshAll();
          } catch (err) {
            vscode.window.showErrorMessage(
              `dbtattic rebuild failed: ${err instanceof Error ? err.message : String(err)}`
            );
          }
        }
      );
    }),

    vscode.commands.registerCommand('dbtattic.copyStatePath', async () => {
      try {
        const path = await cli.statePath(true);
        if (!path) {
          vscode.window.showWarningMessage('dbtattic: no successful invocation in the store yet.');
          return;
        }
        await vscode.env.clipboard.writeText(path);
        vscode.window.showInformationMessage(`dbtattic: copied --state path for the last successful run.`);
      } catch (err) {
        vscode.window.showErrorMessage(
          `dbtattic: ${err instanceof Error ? err.message : String(err)}`
        );
      }
    }),

    vscode.commands.registerCommand('dbtattic.openSettings', async () => {
      await vscode.commands.executeCommand('workbench.action.openSettings', 'dbtattic');
    })
  );

  // The store changes whenever dbt runs; watching it keeps the tree honest
  // without the user having to remember to refresh.
  const watcher = vscode.workspace.createFileSystemWatcher('**/.dbtattic/store.duckdb');
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
