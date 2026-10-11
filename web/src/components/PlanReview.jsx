import React from 'react';
import LineageGraph from './LineageGraph.jsx';

export default function PlanReview({ plan, lineage, onApprove, busy }) {
  if (!plan) return <div className="empty">No plan yet.</div>;

  return (
    <div>
      <div className="section-title">What this project will do</div>
      <p style={{ marginTop: 0 }}>{plan.summary}</p>

      <dl className="kv">
        <dt>Project</dt>
        <dd className="mono">{plan.project_name}</dd>
        <dt>Reads</dt>
        <dd className="mono">{plan.primary_source_table}</dd>
        {plan.additional_source_tables?.length > 0 && (
          <>
            <dt>Joins</dt>
            <dd className="mono">{plan.additional_source_tables.join(', ')}</dd>
          </>
        )}
        <dt>Writes</dt>
        <dd className="mono">{plan.sink_table}</dd>
        <dt>Write mode</dt>
        <dd className="mono">
          {plan.sink_write_mode}
          {plan.merge_keys?.length > 0 && ` on ${plan.merge_keys.join(', ')}`}
        </dd>
        {plan.schedule_cron && (
          <>
            <dt>Schedule</dt>
            <dd className="mono">
              {plan.schedule_cron} {plan.schedule_timezone || ''}
            </dd>
          </>
        )}
      </dl>

      <div className="section-title">How the lineage will look</div>
      <LineageGraph
        nodes={lineage?.nodes}
        edges={lineage?.edges}
        emptyMessage="No lineage context available for these tables."
      />

      {plan.steps?.length > 0 && (
        <>
          <div className="section-title">Steps</div>
          <ol style={{ paddingLeft: 18, margin: 0 }}>
            {plan.steps.map((step, i) => (
              <li key={i} style={{ marginBottom: 6 }}>
                <strong>{step.title}</strong>
                <div className="small">{step.detail}</div>
              </li>
            ))}
          </ol>
        </>
      )}

      {plan.transformation_outline?.length > 0 && (
        <>
          <div className="section-title">Transformation</div>
          <ul style={{ paddingLeft: 18, margin: 0 }} className="small">
            {plan.transformation_outline.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </>
      )}

      {plan.sink_columns?.length > 0 && (
        <>
          <div className="section-title">Output columns ({plan.sink_columns.length})</div>
          <table className="col-table">
            <thead>
              <tr>
                <th>Column</th>
                <th>Type</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {plan.sink_columns.map((column) => (
                <tr key={column.name}>
                  <td className="name">{column.name}</td>
                  <td className="type">{column.data_type}</td>
                  <td className="desc">{column.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="small" style={{ marginTop: 6 }}>
            These become Unity Catalog column comments after the first run.
          </div>
        </>
      )}

      {plan.assumptions?.length > 0 && (
        <>
          <div className="section-title">Assumptions</div>
          <ul style={{ paddingLeft: 18, margin: 0 }} className="small">
            {plan.assumptions.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </>
      )}

      {plan.open_questions?.length > 0 && (
        <>
          <div className="section-title">Open questions</div>
          <ul style={{ paddingLeft: 18, margin: 0 }} className="small">
            {plan.open_questions.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </>
      )}

      <div className="hr" />
      <div className="row">
        <span className="small grow">Ask for changes in the chat, or approve to generate the code.</span>
        <button className="primary" onClick={onApprove} disabled={busy}>
          Approve plan and write code
        </button>
      </div>
    </div>
  );
}
