import React, { useState } from 'react';
import { CodeBlock, DiffBlock } from './CodeView.jsx';

function ValidationSummary({ validation }) {
  if (!validation) return null;
  const errors = validation.errors || [];
  const warnings = validation.warnings || [];

  return (
    <>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className={`badge ${validation.ok ? 'pass' : 'error'}`}>
          {validation.ok ? 'all checks passed' : `${errors.length} failing`}
        </span>
        {warnings.length > 0 && <span className="badge warn">{warnings.length} warnings</span>}
      </div>

      {validation.ok && errors.length === 0 && warnings.length === 0 && (
        <div className="small">
          Passed the framework&apos;s config schema check and transform contract check, a Python
          syntax and undefined-name pass, and a column existence check against the catalog snapshot.
        </div>
      )}

      {(errors.length > 0 || warnings.length > 0) && (
        <ul className="validation-list">
          {errors.map((finding, i) => (
            <li key={`e${i}`}>
              <span className="badge error">{finding.check}</span>
              <span>{finding.message}</span>
            </li>
          ))}
          {warnings.map((finding, i) => (
            <li key={`w${i}`}>
              <span className="badge warn">{finding.check}</span>
              <span>{finding.message}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

export default function CodeReview({ code, previous, onApprove, busy, approved }) {
  const [tab, setTab] = useState('notebook');
  const [showDiff, setShowDiff] = useState(false);

  if (!code) return <div className="empty">No code yet.</div>;

  const hasPrevious = Boolean(previous && (previous.notebook_py || previous.config_yaml));
  const isNotebook = tab === 'notebook';
  const language = isNotebook ? 'python' : 'yaml';
  const current = isNotebook ? code.notebook_py : code.config_yaml;
  const before = isNotebook ? previous?.notebook_py : previous?.config_yaml;
  const filename = isNotebook ? 'transformation_notebook.py' : 'ingestion_config.yaml';

  return (
    <div>
      <div className="section-title">Validation</div>
      <ValidationSummary validation={code.validation} />
      {code.attempts?.length > 1 && (
        <div className="small" style={{ marginTop: 6 }}>
          Repaired automatically {code.attempts.length - 1} time
          {code.attempts.length === 2 ? '' : 's'} before reaching you.
        </div>
      )}

      <div className="section-title">
        projects/{code.project_name}/{filename}
      </div>

      <div className="tabs">
        <button className={`tab ${isNotebook ? 'active' : ''}`} onClick={() => setTab('notebook')}>
          transformation_notebook.py
        </button>
        <button className={`tab ${!isNotebook ? 'active' : ''}`} onClick={() => setTab('config')}>
          ingestion_config.yaml
        </button>
        <span className="grow" />
        {hasPrevious && (
          <button className="ghost" onClick={() => setShowDiff(!showDiff)}>
            {showDiff ? 'Show full file' : 'Show changes'}
          </button>
        )}
      </div>

      {showDiff && hasPrevious ? (
        <DiffBlock before={before || ''} after={current || ''} language={language} />
      ) : (
        <CodeBlock source={current || ''} language={language} />
      )}

      {code.notes && (
        <>
          <div className="section-title">Notes</div>
          <div className="small">{code.notes}</div>
        </>
      )}

      <div className="hr" />
      <div className="row">
        <span className="small grow">
          {approved
            ? 'Approved. Publish it from the panel below.'
            : 'Ask for changes in the chat, or approve to publish.'}
        </span>
        <button
          className="primary"
          onClick={onApprove}
          disabled={busy || approved || !code.validation?.ok}
          title={code.validation?.ok ? '' : 'Validation must pass before approving'}
        >
          Approve code
        </button>
      </div>
    </div>
  );
}
