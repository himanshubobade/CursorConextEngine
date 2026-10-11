// Layered DAG layout, ported from the uc-lineage-web POC.
//
// Longest-path ranking assigns each node a column; three barycenter passes
// then order nodes within a column to reduce edge crossings. Ranking is
// bounded rather than recursive so a cyclic lineage graph terminates - any
// edge that still does not point forward afterwards is a back edge and gets
// routed around rather than drawn right-to-left.

export function computeLayeredLayout(nodeIds, edges) {
  const idSet = new Set(nodeIds);
  const edgeList = edges.filter(
    (e) => idSet.has(e.source) && idSet.has(e.target) && e.source !== e.target
  );

  const rank = {};
  nodeIds.forEach((id) => {
    rank[id] = 0;
  });

  for (let iter = 0; iter < nodeIds.length + 1; iter += 1) {
    let changed = false;
    edgeList.forEach((e) => {
      if (rank[e.target] < rank[e.source] + 1) {
        rank[e.target] = rank[e.source] + 1;
        changed = true;
      }
    });
    if (!changed) break;
  }

  const byRank = {};
  nodeIds.forEach((id) => {
    (byRank[rank[id]] = byRank[rank[id]] || []).push(id);
  });
  const maxRank = Math.max(0, ...Object.keys(byRank).map(Number));

  const order = {};
  Object.keys(byRank).forEach((r) => byRank[r].forEach((id, i) => { order[id] = i; }));

  function barycenterPass(dir) {
    const ranks = Object.keys(byRank)
      .map(Number)
      .sort((a, b) => (dir === 'down' ? a - b : b - a));

    ranks.forEach((r) => {
      const arr = byRank[r];
      const scores = arr.map((id) => {
        const neighbors = edgeList
          .filter((e) => (dir === 'down' ? e.target === id : e.source === id))
          .map((e) => (dir === 'down' ? e.source : e.target))
          .filter((nb) => order[nb] !== undefined && rank[nb] === r + (dir === 'down' ? -1 : 1));
        if (!neighbors.length) return order[id];
        return neighbors.reduce((sum, nb) => sum + order[nb], 0) / neighbors.length;
      });
      const reordered = arr
        .map((_, i) => i)
        .sort((a, b) => scores[a] - scores[b])
        .map((i) => arr[i]);
      byRank[r] = reordered;
      reordered.forEach((id, i) => { order[id] = i; });
    });
  }

  barycenterPass('down');
  barycenterPass('up');
  barycenterPass('down');

  const backEdges = new Set();
  edgeList.forEach((e) => {
    if (rank[e.target] <= rank[e.source]) backEdges.add(e);
  });

  return { rank, byRank, order, maxRank, backEdges, edgeList };
}

export const GEOMETRY = {
  colWidth: 210,
  colGap: 86,
  rowHeight: 74,
  rowGap: 22,
  padX: 28,
  padY: 24,
};

export function computePositions(nodeIds, edges, geometry = GEOMETRY) {
  const { colWidth, colGap, rowHeight, rowGap, padX, padY } = geometry;
  const layout = computeLayeredLayout(nodeIds, edges);
  const positions = {};

  // With no dependency edges there is nothing to rank by, so a single tall
  // column would be the worst possible shape - lay out a grid instead.
  const noStructure = layout.maxRank === 0 && nodeIds.length > 1;
  if (noStructure) {
    const cols = Math.max(1, Math.min(4, Math.ceil(Math.sqrt(nodeIds.length))));
    nodeIds.forEach((id, i) => {
      positions[id] = {
        x: padX + (i % cols) * (colWidth + colGap),
        y: padY + Math.floor(i / cols) * (rowHeight + rowGap),
      };
    });
  } else {
    Object.keys(layout.byRank).forEach((r) => {
      layout.byRank[r].forEach((id, i) => {
        positions[id] = {
          x: padX + Number(r) * (colWidth + colGap),
          y: padY + i * (rowHeight + rowGap),
        };
      });
    });
  }

  const width =
    padX * 2 + (noStructure
      ? Math.min(4, Math.ceil(Math.sqrt(nodeIds.length))) * (colWidth + colGap)
      : (layout.maxRank + 1) * (colWidth + colGap));
  const rows = Math.max(1, ...Object.values(layout.byRank).map((a) => a.length));
  const height = padY * 2 + Math.max(rows, noStructure ? Math.ceil(nodeIds.length / 4) : 1) * (rowHeight + rowGap);

  return { layout, positions, width, height, geometry };
}

// Cubic bezier between two node boxes. Back edges bulge out to the right so
// the arrowhead always advances rather than doubling back over the cards.
export function edgePath(source, target, colWidth, nodeHeight, isBack) {
  const s = { right: source.x + colWidth, midY: source.y + nodeHeight / 2 };
  const t = { left: target.x, right: target.x + colWidth, midY: target.y + nodeHeight / 2 };

  if (!isBack) {
    const dx = Math.max(28, (t.left - s.right) * 0.5);
    return `M ${s.right} ${s.midY} C ${s.right + dx} ${s.midY}, ${t.left - dx} ${t.midY}, ${t.left} ${t.midY}`;
  }
  const bulge = Math.max(s.right, t.right) + 60;
  return `M ${s.right} ${s.midY} C ${bulge} ${s.midY}, ${bulge} ${t.midY}, ${t.right} ${t.midY}`;
}
