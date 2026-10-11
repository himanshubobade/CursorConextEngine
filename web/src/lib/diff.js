// Line diff via longest common subsequence.
//
// Hand-rolled rather than pulled from npm: the inputs are two versions of a
// single generated file, a few hundred lines at most, so the quadratic LCS
// table is far cheaper than the dependency.

export function diffLines(before, after) {
  const a = (before || '').split('\n');
  const b = (after || '').split('\n');

  const table = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }

  const rows = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      rows.push({ type: 'context', text: a[i], oldNo: i + 1, newNo: j + 1 });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      rows.push({ type: 'removed', text: a[i], oldNo: i + 1, newNo: null });
      i += 1;
    } else {
      rows.push({ type: 'added', text: b[j], oldNo: null, newNo: j + 1 });
      j += 1;
    }
  }
  while (i < a.length) {
    rows.push({ type: 'removed', text: a[i], oldNo: i + 1, newNo: null });
    i += 1;
  }
  while (j < b.length) {
    rows.push({ type: 'added', text: b[j], oldNo: null, newNo: j + 1 });
    j += 1;
  }
  return rows;
}

// Collapses long unchanged stretches so a small edit is readable.
export function collapseContext(rows, padding = 3) {
  const keep = new Set();
  rows.forEach((row, index) => {
    if (row.type === 'context') return;
    for (let k = index - padding; k <= index + padding; k += 1) {
      if (k >= 0 && k < rows.length) keep.add(k);
    }
  });
  if (keep.size === 0) return { rows, collapsed: 0 };

  const out = [];
  let skipped = 0;
  let total = 0;
  rows.forEach((row, index) => {
    if (keep.has(index)) {
      if (skipped > 0) {
        out.push({ type: 'gap', text: `${skipped} unchanged line${skipped === 1 ? '' : 's'}` });
        total += skipped;
        skipped = 0;
      }
      out.push(row);
    } else {
      skipped += 1;
    }
  });
  if (skipped > 0) {
    out.push({ type: 'gap', text: `${skipped} unchanged line${skipped === 1 ? '' : 's'}` });
    total += skipped;
  }
  return { rows: out, collapsed: total };
}

export function diffStats(rows) {
  return rows.reduce(
    (acc, row) => {
      if (row.type === 'added') acc.added += 1;
      if (row.type === 'removed') acc.removed += 1;
      return acc;
    },
    { added: 0, removed: 0 }
  );
}
