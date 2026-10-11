import React, { useEffect, useState } from 'react';
import { api } from '../api.js';

const CRON_PRESETS = [
  { label: 'Daily at 02:00', value: '0 0 2 * * ?' },
  { label: 'Hourly', value: '0 0 * * * ?' },
  { label: 'Every 15 minutes', value: '0 0/15 * * * ?' },
  { label: 'Weekly, Monday 03:00', value: '0 0 3 ? * MON' },
  { label: 'Monthly, 1st at 04:00', value: '0 0 4 1 * ?' },
];

function ResultLine({ ok, children }) {
  return (
    <li>
      <span className={`badge ${ok ? 'pass' : 'error'}`}>{ok ? 'ok' : 'failed'}</span>
      <span>{children}</span>
    </li>
  );
}

function Radio({ name, value, checked, onChange, label, hint }) {
  return (
    <label className="row" style={{ gap: 8, alignItems: 'flex-start', cursor: 'pointer' }}>
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        onChange={() => onChange(value)}
        style={{ width: 'auto', marginTop: 3 }}
      />
      <span>
        <span>{label}</span>
        {hint && <div className="small">{hint}</div>}
      </span>
    </label>
  );
}

export default function PublishDeploy({ sessionId, code, plan, publishResult, deployResult, onChanged }) {
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [runState, setRunState] = useState(null);
  const [comments, setComments] = useState(null);

  const [runMode, setRunMode] = useState('scheduled');
  const [compute, setCompute] = useState('serverless');
  const [performanceTarget, setPerformanceTarget] = useState('STANDARD');
  const [cron, setCron] = useState(plan?.schedule_cron || '0 0 2 * * ?');
  const [timezone, setTimezone] = useState(plan?.schedule_timezone || 'Asia/Kolkata');
  const [maxWorkers, setMaxWorkers] = useState(4);
  const [emails, setEmails] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [runNow, setRunNow] = useState(false);

  useEffect(() => {
    api.publishPreview(sessionId).then(setPreview).catch(() => setPreview(null));
    api
      .config()
      .then((cfg) => {
        if (cfg.job_compute) setCompute(cfg.job_compute);
        if (cfg.serverless_performance_target) setPerformanceTarget(cfg.serverless_performance_target);
      })
      .catch(() => {});
  }, [sessionId]);

  useEffect(() => {
    // A one-shot deploy is usually meant to run now. Switching back to
    // scheduled leaves "run immediately" off so you don't double-start.
    setRunNow(runMode === 'once');
  }, [runMode]);

  const guarded = async (name, fn) => {
    setBusy(name);
    setError(null);
    try {
      await fn();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const doPublish = () =>
    guarded('publish', async () => {
      await api.publish(sessionId, { to_git: true, to_workspace: true });
    });

  const doDeploy = () =>
    guarded('deploy', async () => {
      await api.deploy(sessionId, {
        run_mode: runMode,
        quartz_cron_expression: runMode === 'scheduled' ? cron : null,
        timezone_id: runMode === 'scheduled' ? timezone : null,
        max_workers: compute === 'classic' ? Number(maxWorkers) || 4 : null,
        on_failure_emails: emails
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        enabled: runMode === 'scheduled' ? enabled : true,
        run_now: runNow,
        compute,
        performance_target: compute === 'serverless' ? performanceTarget : null,
      });
    });

  const doRun = () =>
    guarded('run', async () => {
      const result = await api.runNow(sessionId);
      setRunState(result);
    });

  const refreshRun = () =>
    guarded('status', async () => {
      const status = await api.deployStatus(sessionId);
      setRunState(status.latest_run);
    });

  const doComments = () =>
    guarded('comments', async () => {
      setComments(await api.applyColumnComments(sessionId));
    });

  const published = publishResult?.ok;

  const deployLabel = () => {
    if (busy === 'deploy') return 'Deploying…';
    if (runMode === 'once') return runNow ? 'Deploy and run once' : 'Create job (manual runs only)';
    return deployResult ? 'Update scheduled job' : 'Create scheduled job';
  };

  const computeSummary = () => {
    const kind = deployResult?.compute || compute;
    if (kind === 'serverless') {
      const target = deployResult?.performance_target || performanceTarget;
      return `Serverless (${target === 'PERFORMANCE_OPTIMIZED' ? 'performance optimized' : 'standard'})`;
    }
    return 'Classic job cluster (14.3, Standard_DS4_v2)';
  };

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}

      <div className="section-title">Publish</div>
      {preview && (
        <div className="small" style={{ marginBottom: 10 }}>
          Writes {preview.git.files.length} files to a{' '}
          <span className="mono">feat/{preview.project_name}</span> branch in{' '}
          <span className="mono">{preview.git.clone}</span> and uploads them to{' '}
          <span className="mono">
            {preview.workspace.root}/projects/{preview.project_name}
          </span>
          . The framework checkout itself is never modified.
          {preview.git.mode === 'seed' && !preview.git.clone_exists && (
            <>
              {' '}
              The framework directory is not a git repository root, so the working copy is created
              as a standalone repository seeded from it.
            </>
          )}
          {preview.git.exists_in_reference && (
            <>
              {' '}
              A project called <span className="mono">{preview.project_name}</span> already exists
              in the framework, so this replaces it on the branch.
            </>
          )}
        </div>
      )}

      {publishResult && (
        <ul className="validation-list" style={{ marginBottom: 10 }}>
          {publishResult.git && (
            <ResultLine ok={publishResult.git.ok}>
              Git: {publishResult.git.detail}
              {publishResult.git.branch && (
                <>
                  {' '}
                  <span className="mono">
                    ({publishResult.git.branch}
                    {publishResult.git.commit ? ` @ ${publishResult.git.commit}` : ''})
                  </span>
                </>
              )}
            </ResultLine>
          )}
          {publishResult.workspace && (
            <ResultLine ok={publishResult.workspace.ok}>
              Workspace: {publishResult.workspace.detail}
            </ResultLine>
          )}
        </ul>
      )}

      <button className="primary" onClick={doPublish} disabled={busy !== null}>
        {busy === 'publish' ? 'Publishing…' : published ? 'Publish again' : 'Publish to repo and workspace'}
      </button>

      <div className="hr" />

      <div className="section-title">Compute</div>
      {!published && (
        <div className="small" style={{ marginBottom: 10 }}>
          Publish first - the job runs the shared driver notebook against the workspace copy of
          this project.
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>
        <Radio
          name="compute"
          value="serverless"
          checked={compute === 'serverless'}
          onChange={setCompute}
          label="Serverless (default)"
          hint="No cluster to start. Databricks sizes the run. Use this unless you need a specific node type."
        />
        <Radio
          name="compute"
          value="classic"
          checked={compute === 'classic'}
          onChange={setCompute}
          label="Classic job cluster"
          hint="The framework template: Spark 14.3, Standard_DS4_v2, 1–4 workers."
        />
      </div>
      {compute === 'serverless' && (
        <div className="field">
          <label>Serverless performance</label>
          <select value={performanceTarget} onChange={(e) => setPerformanceTarget(e.target.value)}>
            <option value="STANDARD">Standard — cheaper, fine for batch loads</option>
            <option value="PERFORMANCE_OPTIMIZED">Performance optimized — faster start, more capacity</option>
          </select>
        </div>
      )}
      {compute === 'classic' && (
        <div className="field" style={{ maxWidth: 160 }}>
          <label>Max workers</label>
          <input type="number" min="1" value={maxWorkers} onChange={(e) => setMaxWorkers(e.target.value)} />
        </div>
      )}

      <div className="section-title">When it runs</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>
        <Radio
          name="runMode"
          value="scheduled"
          checked={runMode === 'scheduled'}
          onChange={setRunMode}
          label="On a schedule"
          hint="Quartz cron on the job. Also written into ingestion_config.yaml so the job and the config cannot drift."
        />
        <Radio
          name="runMode"
          value="once"
          checked={runMode === 'once'}
          onChange={setRunMode}
          label="Once (no schedule)"
          hint="Creates the job with no cron. Trigger it now, or later with Run now. Use this for a one-off CSV load."
        />
      </div>

      {runMode === 'scheduled' && (
        <>
          <div className="field">
            <label>Schedule</label>
            <select
              value={CRON_PRESETS.some((p) => p.value === cron) ? cron : 'custom'}
              onChange={(e) => e.target.value !== 'custom' && setCron(e.target.value)}
            >
              {CRON_PRESETS.map((preset) => (
                <option key={preset.value} value={preset.value}>
                  {preset.label}
                </option>
              ))}
              <option value="custom">Custom</option>
            </select>
          </div>
          <div className="field">
            <label>Quartz cron expression</label>
            <input value={cron} onChange={(e) => setCron(e.target.value)} className="mono" />
          </div>
          <div className="field">
            <label>Timezone</label>
            <input value={timezone} onChange={(e) => setTimezone(e.target.value)} />
          </div>
          <label className="row small" style={{ gap: 6, marginBottom: 10 }}>
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} style={{ width: 'auto' }} />
            Schedule active (uncheck to create the job paused)
          </label>
        </>
      )}

      <div className="field">
        <label>Failure notification emails (comma separated)</label>
        <input value={emails} onChange={(e) => setEmails(e.target.value)} placeholder="dataeng-alerts@example.com" />
      </div>
      <label className="row small" style={{ gap: 6, marginBottom: 10 }}>
        <input type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} style={{ width: 'auto' }} />
        {runMode === 'once'
          ? 'Run it as soon as the job is created'
          : 'Also run once immediately after creating the schedule'}
      </label>

      <button className="primary" onClick={doDeploy} disabled={busy !== null || !published}>
        {deployLabel()}
      </button>

      {deployResult && (
        <>
          <div className="hr" />
          <div className="section-title">Job</div>
          <dl className="kv">
            <dt>Name</dt>
            <dd className="mono">{deployResult.job_name}</dd>
            <dt>Job id</dt>
            <dd className="mono">
              {deployResult.job_url ? (
                <a href={deployResult.job_url} target="_blank" rel="noreferrer">
                  {deployResult.job_id}
                </a>
              ) : (
                deployResult.job_id
              )}
            </dd>
            <dt>Compute</dt>
            <dd>{computeSummary()}</dd>
            <dt>Schedule</dt>
            <dd className="mono">
              {deployResult.schedule?.quartz_cron_expression
                ? `${deployResult.schedule.quartz_cron_expression} (${deployResult.schedule.timezone_id || ''})`
                : 'None — run manually'}
            </dd>
            <dt>Notebook</dt>
            <dd className="mono" style={{ wordBreak: 'break-all' }}>
              {deployResult.notebook_path}
            </dd>
          </dl>

          <div className="row" style={{ marginTop: 10 }}>
            <button onClick={doRun} disabled={busy !== null}>
              {busy === 'run' ? 'Starting…' : 'Run now'}
            </button>
            <button className="ghost" onClick={refreshRun} disabled={busy !== null}>
              Refresh status
            </button>
          </div>

          {runState && (
            <div className="small" style={{ marginTop: 8 }}>
              Run {runState.run_id}: {runState.life_cycle_state || 'pending'}
              {runState.result_state ? ` / ${runState.result_state}` : ''}
              {runState.state_message ? ` - ${runState.state_message}` : ''}
              {runState.run_page_url && (
                <>
                  {' '}
                  <a href={runState.run_page_url} target="_blank" rel="noreferrer">
                    open in Databricks
                  </a>
                </>
              )}
            </div>
          )}

          <div className="hr" />
          <div className="section-title">Describe the new table</div>
          <div className="small" style={{ marginBottom: 8 }}>
            Writes the plan&apos;s {plan?.sink_columns?.length || 0} column descriptions onto{' '}
            <span className="mono">{plan?.sink_table}</span> as Unity Catalog comments. Run this
            after the first successful job run, once the table exists. The next catalog sync picks
            them up, so the next project can find this table.
          </div>
          <button onClick={doComments} disabled={busy !== null}>
            {busy === 'comments' ? 'Applying…' : 'Apply column comments'}
          </button>
          {comments && (
            <div className="small" style={{ marginTop: 8 }}>
              {comments.detail}
              {comments.failed?.length > 0 && (
                <ul style={{ paddingLeft: 18 }}>
                  {comments.failed.map((f) => (
                    <li key={f.column}>
                      {f.column}: {f.error}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
