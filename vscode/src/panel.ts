/**
 * Results webview: renders a QueryResult as a table and keeps an editable SQL
 * box so a result can be drilled into without leaving the panel.
 *
 * All colours come from VS Code's own CSS variables, so the panel follows the
 * user's theme rather than shipping a palette that only works in dark mode.
 */

import * as vscode from 'vscode';
import * as cli from './cli';

let panel: vscode.WebviewPanel | undefined;

function nonce(): string {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from({ length: 32 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
}

function escapeHtml(v: unknown): string {
  if (v === null || v === undefined) {
    return '<span class="null">null</span>';
  }
  return String(v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function isNumeric(v: unknown): boolean {
  return typeof v === 'number';
}

function renderTable(result: cli.QueryResult): string {
  if (result.rows.length === 0) {
    return '<p class="empty">No rows.</p>';
  }
  const head = result.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join('');
  const body = result.rows
    .map(
      (row) =>
        `<tr>${row
          .map((cell) => `<td class="${isNumeric(cell) ? 'num' : ''}">${escapeHtml(cell)}</td>`)
          .join('')}</tr>`
    )
    .join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function html(webview: vscode.Webview, title: string, sql: string, inner: string, rowCount: number): string {
  const n = nonce();
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${n}';">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    background: var(--vscode-editor-background);
    margin: 0; padding: 12px 16px;
  }
  h2 { font-size: 1.05em; margin: 0 0 10px; font-weight: 600; }
  textarea {
    width: 100%; box-sizing: border-box; min-height: 84px; resize: vertical;
    font-family: var(--vscode-editor-font-family, monospace);
    font-size: var(--vscode-editor-font-size, 12px);
    color: var(--vscode-input-foreground);
    background: var(--vscode-input-background);
    border: 1px solid var(--vscode-input-border, transparent);
    border-radius: 3px; padding: 8px;
  }
  textarea:focus { outline: 1px solid var(--vscode-focusBorder); }
  .bar { display: flex; gap: 8px; align-items: center; margin: 8px 0 14px; flex-wrap: wrap; }
  button {
    color: var(--vscode-button-foreground); background: var(--vscode-button-background);
    border: none; padding: 5px 12px; border-radius: 2px; cursor: pointer;
    font-family: inherit; font-size: inherit;
  }
  button:hover { background: var(--vscode-button-hoverBackground); }
  .count { color: var(--vscode-descriptionForeground); font-size: 0.92em; }
  .wrap { overflow-x: auto; border: 1px solid var(--vscode-panel-border, rgba(128,128,128,.25)); border-radius: 3px; }
  table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
  th, td {
    text-align: left; padding: 5px 10px; white-space: nowrap;
    border-bottom: 1px solid var(--vscode-panel-border, rgba(128,128,128,.2));
  }
  th {
    position: sticky; top: 0; font-weight: 600;
    background: var(--vscode-editorWidget-background, var(--vscode-editor-background));
  }
  tbody tr:hover { background: var(--vscode-list-hoverBackground); }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .null { color: var(--vscode-descriptionForeground); font-style: italic; }
  .error {
    color: var(--vscode-errorForeground);
    border: 1px solid var(--vscode-inputValidation-errorBorder, var(--vscode-errorForeground));
    background: var(--vscode-inputValidation-errorBackground, transparent);
    padding: 10px; border-radius: 3px; white-space: pre-wrap;
    font-family: var(--vscode-editor-font-family, monospace);
  }
  .empty { color: var(--vscode-descriptionForeground); }
</style>
</head>
<body>
  <h2>${escapeHtml(title)}</h2>
  <textarea id="sql" spellcheck="false">${escapeHtml(sql)}</textarea>
  <div class="bar">
    <button id="run">Run</button>
    <span class="count">${rowCount >= 0 ? `${rowCount} row${rowCount === 1 ? '' : 's'}` : ''}</span>
    <span class="count">⌘/Ctrl + Enter</span>
  </div>
  <div class="wrap">${inner}</div>
<script nonce="${n}">
  const vscodeApi = acquireVsCodeApi();
  const box = document.getElementById('sql');
  function run() { vscodeApi.postMessage({ type: 'run', sql: box.value }); }
  document.getElementById('run').addEventListener('click', run);
  box.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); run(); }
  });
</script>
</body>
</html>`;
}

async function render(title: string, sql: string): Promise<void> {
  if (!panel) {
    return;
  }
  try {
    const result = await cli.query(sql);
    panel.webview.html = html(panel.webview, title, sql, `<div class="wrap-inner">${renderTable(result)}</div>`, result.rows.length);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    panel.webview.html = html(panel.webview, title, sql, `<pre class="error">${escapeHtml(message)}</pre>`, -1);
  }
}

export async function show(context: vscode.ExtensionContext, title: string, sql: string): Promise<void> {
  if (!panel) {
    panel = vscode.window.createWebviewPanel('dbtattic.results', 'dbtattic', vscode.ViewColumn.Active, {
      enableScripts: true,
      retainContextWhenHidden: true
    });
    panel.onDidDispose(() => (panel = undefined), null, context.subscriptions);
    panel.webview.onDidReceiveMessage(
      async (msg: { type: string; sql: string }) => {
        if (msg.type === 'run') {
          await render('Query', msg.sql);
        }
      },
      null,
      context.subscriptions
    );
  }
  panel.title = `dbtattic · ${title}`;
  panel.reveal(vscode.ViewColumn.Active, true);
  await render(title, sql);
}
