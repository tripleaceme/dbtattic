/**
 * Tree views.
 *
 * Invocations: recent runs, expanding to the nodes that failed or ran slowest.
 * Insights: standing questions history can answer that a single run cannot --
 * runtime regressions, flaky tests, nodes that recently changed.
 */

import * as vscode from 'vscode';
import * as cli from './cli';

type Kind = 'invocation' | 'node' | 'insight' | 'message';

export class Item extends vscode.TreeItem {
  constructor(
    label: string,
    readonly kind: Kind,
    collapsible: vscode.TreeItemCollapsibleState,
    readonly invocationId?: string,
    readonly sql?: string
  ) {
    super(label, collapsible);
    this.contextValue = kind;
  }
}

function statusIcon(failures: number, nodesRun: number): vscode.ThemeIcon {
  if (failures > 0) {
    return new vscode.ThemeIcon('error', new vscode.ThemeColor('testing.iconFailed'));
  }
  if (nodesRun === 0) {
    // A manifest with no run results -- a parse, compile or ls. Worth showing:
    // these are the invocations that silently destroy the previous manifest.
    return new vscode.ThemeIcon('file-code');
  }
  return new vscode.ThemeIcon('pass', new vscode.ThemeColor('testing.iconPassed'));
}

export class InvocationsProvider implements vscode.TreeDataProvider<Item> {
  private emitter = new vscode.EventEmitter<Item | undefined | void>();
  readonly onDidChangeTreeData = this.emitter.event;

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(item: Item): vscode.TreeItem {
    return item;
  }

  async getChildren(item?: Item): Promise<Item[]> {
    try {
      return item ? await this.nodesFor(item) : await this.invocations();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const node = new Item(message, 'message', vscode.TreeItemCollapsibleState.None);
      node.iconPath = new vscode.ThemeIcon('warning');
      return [node];
    }
  }

  private async invocations(): Promise<Item[]> {
    const limit = vscode.workspace.getConfiguration('dbtattic').get<number>('historyLimit') ?? 25;
    const res = await cli.query(
      `select invocation_id, generated_at, coalesce(command,'parse') as command,
              coalesce(target,'-') as target, nodes_run, failures, node_seconds
         from v_run_history limit ${Number(limit)}`
    );

    return res.rows.map((r) => {
      const [id, at, command, target, nodesRun, failures, seconds] = r as [
        string, string, string, string, number, number, number | null
      ];
      const when = String(at).slice(0, 19);
      const item = new Item(
        `${command} · ${when}`,
        'invocation',
        nodesRun > 0 ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None,
        id
      );
      item.description =
        nodesRun > 0
          ? `${target} · ${nodesRun} nodes · ${failures} failed · ${(seconds ?? 0).toFixed(1)}s`
          : `${target} · no nodes executed`;
      item.iconPath = statusIcon(failures, nodesRun);
      item.tooltip = new vscode.MarkdownString(
        [`**invocation** \`${id}\``, ``, `- when: ${at}`, `- command: ${command}`,
         `- target: ${target}`, `- nodes run: ${nodesRun}`, `- failures: ${failures}`].join('\n')
      );
      return item;
    });
  }

  private async nodesFor(item: Item): Promise<Item[]> {
    if (item.kind !== 'invocation' || !item.invocationId) {
      return [];
    }
    // Failures first, then the slowest -- the two reasons to open a run.
    const res = await cli.query(
      `select coalesce(n.name, r.unique_id) as name, r.status, r.execution_time, r.unique_id
         from run_result r
         left join node n using (invocation_id, unique_id)
        where r.invocation_id = '${item.invocationId.replace(/'/g, "''")}'
        order by case when r.status in ('error','fail') then 0 else 1 end,
                 r.execution_time desc
        limit 25`
    );

    return res.rows.map((r) => {
      const [name, status, seconds, uniqueId] = r as [string, string, number | null, string];
      const node = new Item(String(name), 'node', vscode.TreeItemCollapsibleState.None);
      node.description = `${status}${seconds != null ? ` · ${seconds.toFixed(2)}s` : ''}`;
      node.tooltip = uniqueId;
      node.iconPath =
        status === 'error' || status === 'fail'
          ? new vscode.ThemeIcon('error', new vscode.ThemeColor('testing.iconFailed'))
          : status === 'skipped'
            ? new vscode.ThemeIcon('circle-slash')
            : new vscode.ThemeIcon('check');
      return node;
    });
  }
}

interface Insight {
  label: string;
  detail: string;
  icon: string;
  sql: string;
}

/** The questions a single run cannot answer -- which is the whole point of keeping history. */
const INSIGHTS: Insight[] = [
  {
    label: 'Runtime regressions',
    detail: 'slowest vs their own median',
    icon: 'graph-line',
    sql: `with stats as (
  select unique_id, name,
         median(execution_time) as median_s,
         max(execution_time)    as worst_s,
         count(*)               as runs
    from v_node_runtime
   where execution_time is not null
   group by all
  having count(*) >= 3
)
select name, round(median_s,3) as median_s, round(worst_s,3) as worst_s,
       round(worst_s / nullif(median_s,0), 1) as x_slower, runs
  from stats
 where worst_s > median_s * 1.5
 order by x_slower desc`
  },
  {
    label: 'Flaky and skipped tests',
    detail: 'failure rate across runs',
    icon: 'beaker',
    sql: `select name, runs, passes, failures, skipped, failure_pct, last_seen
  from v_test_history
 where failures > 0 or skipped > 0
 order by failure_pct desc nulls last, skipped desc`
  },
  {
    label: 'Recently changed models',
    detail: 'content hash changed between runs',
    icon: 'git-commit',
    sql: `select name, resource_type, node_version_id[1:8] as version,
       first_seen, last_seen, invocations
  from v_node_changes
 where unique_id in (
   select unique_id from v_node_changes group by unique_id having count(*) > 1
 )
 order by first_seen desc`
  },
  {
    label: 'Invocations that destroyed a manifest',
    detail: 'parse / compile / ls runs',
    icon: 'warning',
    sql: `select invocation_id, generated_at, command, target
  from v_run_history
 where nodes_run = 0
 order by generated_at desc`
  },
  {
    label: 'Store contents',
    detail: 'what is captured, and de-duplication',
    icon: 'database',
    sql: `select 'invocations' as metric, count(*)::varchar as value from invocation
union all select 'artifacts captured', count(*)::varchar from artifact_capture
union all select 'node versions', count(*)::varchar from node_version
union all select 'node rows', count(*)::varchar from node_invocation
union all select 'dedup ratio',
       round((select count(*) from node_invocation)::double
             / nullif((select count(*) from node_version),0), 1)::varchar || 'x'`
  }
];

export class InsightsProvider implements vscode.TreeDataProvider<Item> {
  private emitter = new vscode.EventEmitter<Item | undefined | void>();
  readonly onDidChangeTreeData = this.emitter.event;

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(item: Item): vscode.TreeItem {
    return item;
  }

  async getChildren(): Promise<Item[]> {
    return INSIGHTS.map((i) => {
      const item = new Item(i.label, 'insight', vscode.TreeItemCollapsibleState.None, undefined, i.sql);
      item.description = i.detail;
      item.iconPath = new vscode.ThemeIcon(i.icon);
      item.command = {
        command: 'dbtattic.runInsight',
        title: 'Run',
        arguments: [i.label, i.sql]
      };
      return item;
    });
  }
}
