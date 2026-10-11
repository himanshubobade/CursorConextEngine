import React, { useMemo } from 'react';
import { collapseContext, diffLines, diffStats } from '../lib/diff.js';
import { tokenize } from '../lib/highlight.js';

function Line({ text, language }) {
  return (
    <>
      {tokenize(text, language).map((token, i) => (
        <span key={i} className={token.cls || undefined}>
          {token.text}
        </span>
      ))}
    </>
  );
}

export function CodeBlock({ source, language }) {
  const lines = useMemo(() => (source || '').split('\n'), [source]);
  return (
    <div className="code-block">
      {lines.map((text, i) => (
        <div className="code-line" key={i}>
          <span className="ln">{i + 1}</span>
          <span className="lc">
            <Line text={text} language={language} />
          </span>
        </div>
      ))}
    </div>
  );
}

export function DiffBlock({ before, after, language }) {
  const { rows, stats } = useMemo(() => {
    const raw = diffLines(before, after);
    const collapsed = collapseContext(raw);
    return { rows: collapsed.rows, stats: diffStats(raw) };
  }, [before, after]);

  return (
    <>
      <div className="small" style={{ marginBottom: 6 }}>
        <span style={{ color: 'var(--green)' }}>+{stats.added}</span>{' '}
        <span style={{ color: 'var(--red)' }}>-{stats.removed}</span> against the previous version
      </div>
      <div className="code-block">
        {rows.map((row, i) => {
          if (row.type === 'gap') {
            return (
              <div className="code-line" key={i}>
                <span className="ln">…</span>
                <span className="lc" style={{ color: 'var(--muted-2)' }}>
                  {row.text}
                </span>
              </div>
            );
          }
          return (
            <div className={`code-line ${row.type}`} key={i}>
              <span className="ln">{row.newNo ?? row.oldNo}</span>
              <span className="lc">
                <Line text={row.text} language={language} />
              </span>
            </div>
          );
        })}
      </div>
    </>
  );
}
