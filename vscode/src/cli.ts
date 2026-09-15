/**
 * Thin wrapper over the dbtattic CLI.
 *
 * The extension deliberately does NOT embed DuckDB. Shelling out to the CLI
 * keeps the extension free of native dependencies, and keeps one definition of
 * the schema and the views rather than a second copy that can drift.
 */

import { execFile } from 'child_process';
import * as vscode from 'vscode';

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
}

export interface StoreInfo {
  project_dir: string;
  store: string;
  store_source: string;
  db_path: string;
  archive_dir: string;
  target_path: string;
  capture_when: string;
  artifacts: string[];
  exists: boolean;
  invocations?: number;
  artifacts_captured?: number;
  node_versions?: number;
  node_rows?: number;
  stale?: boolean;
  message?: string;
}

export class CliError extends Error {
  constructor(message: string, readonly stderr: string, readonly code: number | null) {
    super(message);
  }
}

function config() {
  return vscode.workspace.getConfiguration('dbtattic');
}

function cwd(): string | undefined {
  const configured = config().get<string>('projectDir');
  if (configured) {
    return configured;
  }
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

export function run(args: string[], timeoutMs = 60_000): Promise<string> {
  const bin = config().get<string>('cliPath') || 'dbtattic';
  return new Promise((resolve, reject) => {
    execFile(
      bin,
      args,
      { cwd: cwd(), timeout: timeoutMs, maxBuffer: 32 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          const code = (err as NodeJS.ErrnoException & { code?: number }).code ?? null;
          if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
            reject(
              new CliError(
                `dbtattic executable not found at "${bin}". Install it with \`pip install dbtattic\`, or set dbtattic.cliPath to your virtualenv's bin/dbtattic.`,
                stderr,
                null
              )
            );
            return;
          }
          // The CLI reports query errors as JSON on stdout with a non-zero exit,
          // so a failure with parseable stdout is still a usable result.
          if (stdout.trim().startsWith('{')) {
            resolve(stdout);
            return;
          }
          reject(new CliError(stderr.trim() || err.message, stderr, typeof code === 'number' ? code : null));
          return;
        }
        resolve(stdout);
      }
    );
  });
}

export async function query(sql: string, limit = 500): Promise<QueryResult> {
  const out = await run(['query', '--json', '--limit', String(limit), sql]);
  const parsed = JSON.parse(out);
  if (parsed.error) {
    throw new Error(parsed.error);
  }
  return parsed as QueryResult;
}

export async function info(): Promise<StoreInfo> {
  const out = await run(['info', '--json']);
  return JSON.parse(out) as StoreInfo;
}

export async function capture(): Promise<void> {
  await run(['capture', '--quiet', '--phase', 'manual']);
}

export async function rebuild(): Promise<string> {
  return run(['rebuild'], 300_000);
}

export async function statePath(lastSuccess = true): Promise<string> {
  const args = ['state'];
  if (lastSuccess) {
    args.push('--last-success');
  }
  // `state` keeps stdout clean precisely so it can be captured like this.
  const out = await run(args);
  return out.trim();
}
