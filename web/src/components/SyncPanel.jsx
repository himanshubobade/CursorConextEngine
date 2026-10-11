import React, { useState } from 'react';
import { api } from '../api.js';

function percent(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

export default function SyncPanel({ summary, onRefresh }) {
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState(null);

  const snapshot = summary?.snapshot || {};

  const runSync = async (full) => {
    setRunning(true);
    setError(null);
    setProgress('Starting');
    try {
      await api.sync({ embed: true, full_lineage: full }, (event) => {
        if (event.type === 'progress') {
          const parts = [event.stage];
          if (event.catalog) parts.push(event.catalog);
          if (event.phase) parts.push(event.phase);
          if (event.done !== undefined && event.stale !== undefined) {
            parts.push(`${event.done}/${event.stale}`);
          }
          setProgress(parts.filter(Boolean).join(' · '));
        } else if (event.type === 'complete') {
          setProgress(
            `${event.report.tables} tables, ${event.report.columns} columns, ` +
              `${event.report.lineage?.edges_seen || 0} lineage edges in ${event.report.duration_seconds}s`
          );
        } else if (event.type === 'error') {
          setError(event.message);
        }
      });
      onRefresh?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="sidebar-section">
      <h2>Catalog snapshot</h2>
      {snapshot.tables ? (
        <div className="small">
          {snapshot.tables.toLocaleString()} tables · {snapshot.columns.toLocaleString()} columns
          <br />
          {snapshot.lineage_edges.toLocaleString()} lineage edges
          <br />
          {snapshot.embedded_docs.toLocaleString()} embedded docs
          <br />
          <span title="Share of columns that have a description. Retrieval quality tracks this closely.">
            {percent(snapshot.column_comment_coverage)} of columns described
          </span>
        </div>
      ) : (
        <div className="small">
          Nothing indexed yet. Sync a catalog to enable table discovery.
        </div>
      )}

      <div className="row" style={{ marginTop: 8 }}>
        <button className="grow" onClick={() => runSync(false)} disabled={running}>
          {running ? 'Syncing…' : 'Sync catalog'}
        </button>
        <button
          className="ghost"
          onClick={() => runSync(true)}
          disabled={running}
          title="Rebuild lineage from the full lookback window instead of since the last sync"
        >
          Full
        </button>
      </div>

      {progress && (
        <div className="small" style={{ marginTop: 6, color: 'var(--muted-2)' }}>
          {progress}
        </div>
      )}
      {error && (
        <div className="small" style={{ marginTop: 6, color: 'var(--red)' }}>
          {error}
        </div>
      )}
    </div>
  );
}
