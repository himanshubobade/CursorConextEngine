import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import ChatPanel from './components/ChatPanel.jsx';
import CodeReview from './components/CodeReview.jsx';
import CostMeter from './components/CostMeter.jsx';
import ModelPicker from './components/ModelPicker.jsx';
import PlanReview from './components/PlanReview.jsx';
import PublishDeploy from './components/PublishDeploy.jsx';
import SyncPanel from './components/SyncPanel.jsx';
import TableReview from './components/TableReview.jsx';

const WORKSPACE_TITLES = {
  intake: 'Context',
  table_review: 'Candidate tables',
  plan_review: 'Execution plan',
  code_review: 'Generated project',
  publish_ready: 'Publish and deploy',
  deployed: 'Publish and deploy',
};

function HealthDot({ health }) {
  if (!health) return <span className="dot" title="Checking" />;
  const level = health.ready ? 'ok' : health.framework?.exists ? 'warn' : 'bad';
  const detail = [
    `Databricks: ${health.databricks?.status}`,
    `Framework: ${health.framework?.exists ? 'found' : 'missing'}`,
    `LLM keys: ${Object.entries(health.llm_providers || {})
      .filter(([, v]) => v.configured)
      .map(([k]) => k)
      .join(', ') || 'none'}`,
  ].join('\n');
  return <span className={`dot ${level}`} title={detail} />;
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [summary, setSummary] = useState(null);
  const [models, setModels] = useState([]);
  const [config, setConfig] = useState(null);

  const [sessions, setSessions] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [session, setSession] = useState(null);
  const [messages, setMessages] = useState([]);
  const [usage, setUsage] = useState(null);

  const [modelId, setModelId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);

  // Kept so the code panel can diff the new version against the last one.
  const previousCode = useRef(null);

  const refreshMeta = useCallback(() => {
    api.health().then(setHealth).catch(() => {});
    api.catalogSummary().then(setSummary).catch(() => {});
  }, []);

  useEffect(() => {
    refreshMeta();
    api.models().then((r) => setModels(r.models || [])).catch(() => {});
    api.config().then(setConfig).catch(() => {});
    api.sessions().then((r) => setSessions(r.sessions || [])).catch(() => {});
  }, [refreshMeta]);

  const loadSession = useCallback(async (id) => {
    const data = await api.session(id);
    setSession(data);
    setMessages(data.messages || []);
    setUsage(data.usage || null);
    setModelId(data.state?.model_id || null);
    return data;
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    loadSession(sessionId).catch((e) => setError(e.message));
  }, [sessionId, loadSession]);

  const newSession = async () => {
    const created = await api.createSession({ model_id: modelId });
    previousCode.current = null;
    setSessions((prev) => [{ id: created.id, title: 'New project', stage: 'intake' }, ...prev]);
    setSessionId(created.id);
  };

  const removeSession = async (id, event) => {
    event.stopPropagation();
    await api.deleteSession(id);
    setSessions((prev) => prev.filter((s) => s.id !== id));
    if (id === sessionId) {
      setSessionId(null);
      setSession(null);
      setMessages([]);
    }
  };

  /**
   * Drives one turn of the pipeline. Events arrive as they happen so the
   * analyst sees which stage is running rather than a single long spinner.
   */
  const advance = async (body) => {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    setStatus('Working');

    if (body.action === 'message' && body.text) {
      setMessages((prev) => [
        ...prev,
        { id: `local-${Date.now()}`, role: 'user', content: body.text, stage: session?.stage },
      ]);
    }
    if (session?.state?.code) previousCode.current = session.state.code;

    try {
      await api.advance(sessionId, { ...body, model_id: modelId }, (event) => {
        switch (event.type) {
          case 'status':
            setStatus(event.message);
            break;
          case 'message':
            setMessages((prev) => [
              ...prev,
              { id: `srv-${Date.now()}-${prev.length}`, role: event.role, content: event.content, stage: event.stage },
            ]);
            break;
          case 'stage':
            setSession((prev) => (prev ? { ...prev, stage: event.stage } : prev));
            break;
          case 'usage_total':
            setUsage(event);
            break;
          case 'error':
            setError(event.message);
            break;
          default:
            break;
        }
      });
    } catch (e) {
      setError(e.message);
    } finally {
      setStatus(null);
      setBusy(false);
      // Reload so the workspace panel reads the persisted artifacts rather
      // than a partially applied stream.
      try {
        const data = await loadSession(sessionId);
        setSessions((prev) =>
          prev.map((s) => (s.id === sessionId ? { ...s, title: data.title, stage: data.stage } : s))
        );
      } catch {
        /* the error is already surfaced */
      }
    }
  };

  const setSelection = async (tableIds) => {
    setSession((prev) =>
      prev ? { ...prev, state: { ...prev.state, selected_tables: tableIds } } : prev
    );
    await api.setTables(sessionId, tableIds).catch((e) => setError(e.message));
  };

  const state = session?.state || {};
  const stage = session?.stage || 'intake';

  const workspace = () => {
    if (!session) {
      return (
        <div className="empty">
          Start a project to see candidate tables, the execution plan, the generated code, and the
          deploy controls here.
        </div>
      );
    }

    if (stage === 'table_review') {
      return (
        <TableReview
          candidates={state.candidates || []}
          selected={state.selected_tables || []}
          onSelectionChange={setSelection}
          onApprove={() => advance({ action: 'approve_tables', table_ids: state.selected_tables || [] })}
          busy={busy}
        />
      );
    }

    if (stage === 'plan_review') {
      return (
        <PlanReview
          plan={state.plan}
          lineage={state.projected_lineage}
          onApprove={() => advance({ action: 'approve_plan' })}
          busy={busy}
        />
      );
    }

    if (stage === 'code_review') {
      return (
        <CodeReview
          code={state.code}
          previous={previousCode.current}
          onApprove={() => advance({ action: 'approve_code' })}
          busy={busy}
        />
      );
    }

    if (stage === 'publish_ready' || stage === 'deployed') {
      return (
        <>
          <PublishDeploy
            sessionId={sessionId}
            code={state.code}
            plan={state.plan}
            publishResult={state.publish}
            deployResult={state.deploy}
            onChanged={() => loadSession(sessionId)}
          />
          <div className="hr" />
          <CodeReview code={state.code} previous={previousCode.current} approved busy={busy} onApprove={() => {}} />
        </>
      );
    }

    // intake
    return (
      <div>
        <div className="section-title">What I know</div>
        <div className="small">
          <p style={{ marginTop: 0 }}>
            <strong>Framework:</strong>{' '}
            {summary?.framework?.available
              ? `${summary.framework.examples?.length || 0} example projects, ` +
                `${summary.framework.source_types?.length || 0} source types, ` +
                `${summary.framework.sink_types?.length || 0} sink types`
              : 'not found - check paths.framework_repo in settings.yaml'}
          </p>
          <p>
            <strong>Catalog:</strong>{' '}
            {summary?.snapshot?.tables
              ? `${summary.snapshot.tables.toLocaleString()} tables across ${
                  summary.snapshot.catalogs?.join(', ') || '—'
                }`
              : 'no snapshot yet - run a catalog sync from the left'}
          </p>
          <p>
            <strong>Search index:</strong>{' '}
            {summary?.vector_index_size
              ? `${summary.vector_index_size.toLocaleString()} embedded documents`
              : 'empty'}
          </p>
        </div>
        {!health?.ready && (
          <div className="notice" style={{ marginTop: 12 }}>
            Some setup is incomplete. Hover the status dot next to the title for details, and see{' '}
            <span className="mono">secrets.example.yaml</span>.
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <h1>ContextEngine</h1>
          <HealthDot health={health} />
        </div>

        <div className="sidebar-section">
          <h2>Model</h2>
          <ModelPicker
            models={models}
            value={modelId}
            onChange={setModelId}
            stageDefaults={config?.stage_models}
          />
          {usage && (
            <div style={{ marginTop: 10 }}>
              <CostMeter usage={usage} />
            </div>
          )}
        </div>

        <SyncPanel summary={summary} onRefresh={refreshMeta} />

        <div className="sidebar-section">
          <button className="primary" style={{ width: '100%' }} onClick={newSession}>
            New project
          </button>
        </div>

        <div className="session-list">
          {sessions.map((item) => (
            <div
              key={item.id}
              className={`session-item ${item.id === sessionId ? 'active' : ''}`}
              onClick={() => setSessionId(item.id)}
            >
              <div className="row">
                <div className="title grow">{item.title || 'Untitled'}</div>
                <button className="ghost" style={{ padding: '0 4px' }} onClick={(e) => removeSession(item.id, e)}>
                  ×
                </button>
              </div>
              <div className="meta">{item.stage?.replace('_', ' ')}</div>
            </div>
          ))}
          {sessions.length === 0 && <div className="empty">No projects yet.</div>}
        </div>
      </aside>

      <ChatPanel
        session={session}
        messages={messages}
        statusLine={status}
        busy={busy}
        error={error}
        onSend={(text) => advance({ action: 'message', text })}
        header={
          session ? (
            <>
              <h2>{session.title || 'New project'}</h2>
              <span className="spacer" />
            </>
          ) : (
            <h2>No project selected</h2>
          )
        }
      />

      <section className="workspace">
        <div className="pane-header">
          <h2>{WORKSPACE_TITLES[stage] || 'Workspace'}</h2>
        </div>
        <div className="pane-body">{workspace()}</div>
      </section>
    </div>
  );
}
