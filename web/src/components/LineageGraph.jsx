import React, { useMemo } from 'react';
import { computePositions, edgePath, GEOMETRY } from '../lib/layout.js';

const NODE_HEIGHT = 56;

function shortName(id) {
  const parts = String(id).split('.');
  return parts[parts.length - 1] || id;
}

function qualifier(node) {
  if (node.catalog && node.schema) return `${node.catalog}.${node.schema}`;
  const parts = String(node.id).split('.');
  return parts.length === 3 ? `${parts[0]}.${parts[1]}` : '';
}

/**
 * Renders a lineage graph as absolutely positioned cards with SVG bezier
 * connectors underneath - the same approach as the uc-lineage-web POC, which
 * keeps nodes as real DOM (selectable text, hover, click) instead of canvas.
 *
 * Proposed nodes and edges are drawn dashed so a plan preview is visually
 * distinguishable from lineage Unity Catalog has actually recorded.
 */
export default function LineageGraph({ nodes, edges, onNodeClick, emptyMessage }) {
  const { positions, layout, width, height, nodeById } = useMemo(() => {
    const ids = (nodes || []).map((n) => n.id);
    const byId = Object.fromEntries((nodes || []).map((n) => [n.id, n]));
    const computed = computePositions(ids, edges || [], GEOMETRY);
    return { ...computed, nodeById: byId };
  }, [nodes, edges]);

  if (!nodes || nodes.length === 0) {
    return <div className="empty">{emptyMessage || 'Nothing to show yet.'}</div>;
  }

  const edgeKey = (e, i) => `${e.source}->${e.target}-${i}`;

  return (
    <>
      <div className="graph-wrap">
        <div className="graph-inner" style={{ width, height }}>
          <svg className="graph-svg" width={width} height={height}>
            <defs>
              <marker id="arrow-solid" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill="#4c5565" />
              </marker>
              <marker id="arrow-proposed" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill="#3fb950" />
              </marker>
            </defs>
            {(layout.edgeList || []).map((e, i) => {
              const source = positions[e.source];
              const target = positions[e.target];
              if (!source || !target) return null;
              const proposed = e.status === 'proposed';
              return (
                <path
                  key={edgeKey(e, i)}
                  d={edgePath(source, target, GEOMETRY.colWidth, NODE_HEIGHT, layout.backEdges.has(e))}
                  stroke={proposed ? '#3fb950' : '#39404e'}
                  strokeWidth="1.5"
                  strokeDasharray={proposed ? '5 4' : undefined}
                  fill="none"
                  markerEnd={`url(#${proposed ? 'arrow-proposed' : 'arrow-solid'})`}
                />
              );
            })}
          </svg>

          {nodes.map((node) => {
            const pos = positions[node.id];
            if (!pos) return null;
            const role = node.role || 'context';
            const clickable = Boolean(onNodeClick) && node.known !== false;
            return (
              <div
                key={node.id}
                className={`graph-node role-${role}${clickable ? ' clickable' : ''}`}
                style={{ left: pos.x, top: pos.y, width: GEOMETRY.colWidth, minHeight: NODE_HEIGHT }}
                onClick={clickable ? () => onNodeClick(node) : undefined}
                title={node.id}
              >
                {role === 'sink' && <span className="n-tag">new</span>}
                {role === 'source' && <span className="n-tag">source</span>}
                <div className="n-label">{shortName(node.id)}</div>
                <div className="n-sub">{qualifier(node)}</div>
              </div>
            );
          })}
        </div>
      </div>
      <div className="graph-legend">
        <span><span className="swatch" style={{ background: '#2b4f8a' }} />source table</span>
        <span><span className="swatch" style={{ background: '#1e3d24', border: '1px dashed #3fb950' }} />table this project creates</span>
        <span><span className="swatch" style={{ background: '#1c2029' }} />existing lineage context</span>
      </div>
    </>
  );
}
