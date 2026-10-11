import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import LineageGraph from './LineageGraph.jsx';

function scoreClass(relevance) {
  if (relevance === undefined || relevance === null) return '';
  if (relevance >= 0.7) return 'high';
  if (relevance >= 0.4) return 'mid';
  return '';
}

function TableCard({ card, selected, onToggle, onInspect }) {
  const relevance = card.relevance;
  return (
    <div className={`card ${selected ? 'selected' : ''}`}>
      <div className="card-head">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggle}
          style={{ width: 'auto', marginTop: 3 }}
        />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="card-title">{card.table_id}</div>
          {card.comment && <div className="card-sub">{card.comment}</div>}
          {card.reason && <div className="card-reason">{card.reason}</div>}
          {card.matched_columns?.length > 0 && (
            <div className="chips">
              {card.matched_columns.slice(0, 8).map((column) => (
                <span className="chip match" key={column}>
                  {column}
                </span>
              ))}
            </div>
          )}
          <div className="row small" style={{ marginTop: 8, gap: 12 }}>
            <span>{card.column_count} columns</span>
            {card.signals?.graph_distance !== undefined && (
              <span title={`Reached from ${(card.signals.reached_from || []).join(', ')}`}>
                {card.signals.graph_distance} hop{card.signals.graph_distance === 1 ? '' : 's'} in lineage
              </span>
            )}
            <button className="ghost" style={{ padding: '2px 8px' }} onClick={() => onInspect(card.table_id)}>
              Inspect
            </button>
          </div>
        </div>
        {relevance !== undefined && (
          <span className={`score ${scoreClass(relevance)}`}>{relevance.toFixed(2)}</span>
        )}
      </div>
    </div>
  );
}

function TableInspector({ tableId, onClose }) {
  const [detail, setDetail] = useState(null);
  const [lineage, setLineage] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setLineage(null);
    setError(null);
    Promise.all([api.tableDetail(tableId), api.tableLineage(tableId, 1)])
      .then(([d, l]) => {
        if (cancelled) return;
        setDetail(d);
        setLineage(l);
      })
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [tableId]);

  const graphNodes = lineage
    ? [
        ...lineage.upstream.map((n) => ({ ...n, role: 'context' })),
        { ...lineage.table, role: 'source' },
        ...lineage.downstream.map((n) => ({ ...n, role: 'context' })),
      ]
    : [];

  return (
    <div>
      <div className="row" style={{ marginBottom: 10 }}>
        <div className="card-title grow">{tableId}</div>
        <button className="ghost" onClick={onClose}>
          Back to candidates
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {lineage && (
        <>
          <div className="section-title">Lineage</div>
          <LineageGraph nodes={graphNodes} edges={lineage.edges} emptyMessage="No recorded lineage for this table." />
        </>
      )}

      {detail && (
        <>
          <div className="section-title">Columns ({detail.columns.length})</div>
          <table className="col-table">
            <thead>
              <tr>
                <th>Column</th>
                <th>Type</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {detail.columns.map((column) => (
                <tr key={column.column_name}>
                  <td className="name">{column.column_name}</td>
                  <td className="type">{column.data_type}</td>
                  <td className="desc">{column.comment || <span style={{ color: 'var(--muted-2)' }}>—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export default function TableReview({ candidates, selected, onSelectionChange, onApprove, busy }) {
  const [inspecting, setInspecting] = useState(null);
  const [query, setQuery] = useState('');
  const [found, setFound] = useState([]);
  const [searching, setSearching] = useState(false);

  const selectedSet = new Set(selected);
  const known = new Map(candidates.map((c) => [c.table_id, c]));
  found.forEach((c) => {
    if (!known.has(c.table_id)) known.set(c.table_id, c);
  });
  const cards = Array.from(known.values());

  const toggle = (tableId) => {
    const next = selectedSet.has(tableId)
      ? selected.filter((id) => id !== tableId)
      : [...selected, tableId];
    onSelectionChange(next);
  };

  const search = async () => {
    if (query.trim().length < 2) return;
    setSearching(true);
    try {
      const result = await api.searchTables(query.trim(), 8);
      setFound(result.results || []);
    } finally {
      setSearching(false);
    }
  };

  if (inspecting) {
    return <TableInspector tableId={inspecting} onClose={() => setInspecting(null)} />;
  }

  return (
    <div>
      <div className="notice" style={{ marginBottom: 12 }}>
        These are the tables I think this pipeline needs. Tick the ones to use, add anything I
        missed, then continue. Everything downstream is built only from what you confirm here.
      </div>

      {cards.map((card) => (
        <TableCard
          key={card.table_id}
          card={card}
          selected={selectedSet.has(card.table_id)}
          onToggle={() => toggle(card.table_id)}
          onInspect={setInspecting}
        />
      ))}

      {cards.length === 0 && <div className="empty">No candidates yet.</div>}

      <div className="section-title">Add a table I missed</div>
      <div className="row">
        <input
          className="grow"
          value={query}
          placeholder="Search the catalog by name, column or meaning"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && search()}
        />
        <button onClick={search} disabled={searching || query.trim().length < 2}>
          {searching ? 'Searching…' : 'Search'}
        </button>
      </div>

      <div className="hr" />
      <div className="row">
        <span className="small grow">
          {selected.length} table{selected.length === 1 ? '' : 's'} selected
        </span>
        <button className="primary" onClick={onApprove} disabled={busy || selected.length === 0}>
          Continue with these tables
        </button>
      </div>
    </div>
  );
}
