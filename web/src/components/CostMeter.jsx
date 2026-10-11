import React from 'react';

function formatTokens(n) {
  if (!n) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export default function CostMeter({ usage }) {
  if (!usage) return null;
  const spent = usage.cost_usd || 0;
  const budget = usage.budget_usd || 0;
  const fraction = budget ? Math.min(1, spent / budget) : 0;
  const level = fraction >= 1 ? 'over' : fraction >= 0.75 ? 'warn' : '';
  const cached = usage.cache_read_tokens || 0;

  return (
    <div className="cost-meter">
      <div className="row">
        <span className="grow">
          ${spent.toFixed(4)}
          {budget ? ` of $${budget.toFixed(2)}` : ''}
        </span>
        <span>{usage.calls || 0} calls</span>
      </div>
      {budget > 0 && (
        <div className="bar">
          <div className={`fill ${level}`} style={{ width: `${fraction * 100}%` }} />
        </div>
      )}
      <div className="row" style={{ marginTop: 4 }}>
        <span className="grow">
          {formatTokens(usage.input_tokens)} in · {formatTokens(usage.output_tokens)} out
        </span>
        {cached > 0 && <span title="Billed at the cached rate">{formatTokens(cached)} cached</span>}
      </div>
    </div>
  );
}
