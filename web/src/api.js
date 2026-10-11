// Thin fetch layer. Streaming endpoints are POSTs, so they read the body
// manually rather than using EventSource (which is GET-only).

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body.message || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

async function readSSE(res, onEvent) {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf('\n\n')) !== -1) {
      const chunk = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          onEvent(JSON.parse(payload));
        } catch {
          /* ignore a partial frame */
        }
      }
    }
  }
}

export const api = {
  health: () => request('/api/health'),
  config: () => request('/api/config'),
  models: () => request('/api/models'),

  catalogSummary: () => request('/api/catalog/summary'),
  catalogs: () => request('/api/catalog/catalogs'),
  schemas: (catalog) => request(`/api/catalog/${encodeURIComponent(catalog)}/schemas`),
  schemaTables: (catalog, schema) =>
    request(`/api/catalog/${encodeURIComponent(catalog)}/schema/${encodeURIComponent(schema)}/tables`),
  tableDetail: (tableId) => request(`/api/catalog/table/${encodeURIComponent(tableId)}`),
  tableLineage: (tableId, hops = 1) =>
    request(`/api/catalog/table/${encodeURIComponent(tableId)}/lineage?hops=${hops}`),
  searchTables: (q, topK = 10) =>
    request(`/api/catalog/search?q=${encodeURIComponent(q)}&top_k=${topK}`),

  sessions: () => request('/api/sessions'),
  createSession: (body) => request('/api/sessions', { method: 'POST', body: JSON.stringify(body || {}) }),
  session: (id) => request(`/api/sessions/${id}`),
  deleteSession: (id) => request(`/api/sessions/${id}`, { method: 'DELETE' }),
  setTables: (id, tableIds) =>
    request(`/api/sessions/${id}/tables`, { method: 'PUT', body: JSON.stringify({ table_ids: tableIds }) }),

  publishPreview: (id) => request(`/api/publish/${id}/preview`),
  publish: (id, body) => request(`/api/publish/${id}`, { method: 'POST', body: JSON.stringify(body || {}) }),

  deploy: (id, body) => request(`/api/deploy/${id}`, { method: 'POST', body: JSON.stringify(body || {}) }),
  runNow: (id) => request(`/api/deploy/${id}/run`, { method: 'POST', body: '{}' }),
  deployStatus: (id) => request(`/api/deploy/${id}/status`),
  runState: (runId) => request(`/api/deploy/runs/${runId}`),
  applyColumnComments: (id) =>
    request(`/api/deploy/${id}/column-comments`, { method: 'POST', body: '{}' }),

  async advance(sessionId, body, onEvent) {
    const res = await fetch(`/api/sessions/${sessionId}/advance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    await readSSE(res, onEvent);
  },

  async sync(body, onEvent) {
    const res = await fetch('/api/catalog/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    await readSSE(res, onEvent);
  },
};
