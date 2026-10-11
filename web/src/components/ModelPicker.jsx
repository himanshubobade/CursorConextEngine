import React from 'react';

export default function ModelPicker({ models, value, onChange, stageDefaults }) {
  const grouped = models.reduce((acc, model) => {
    (acc[model.provider] = acc[model.provider] || []).push(model);
    return acc;
  }, {});

  const defaultLabel = stageDefaults
    ? `Per stage (${stageDefaults.plan || '?'} for planning, ${stageDefaults.codegen || '?'} for code)`
    : 'Per-stage defaults';

  return (
    <div className="model-picker">
      <select value={value || ''} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">{defaultLabel}</option>
        {Object.entries(grouped).map(([provider, entries]) => (
          <optgroup key={provider} label={provider}>
            {entries.map((model) => (
              <option key={model.id} value={model.id} disabled={!model.available}>
                {model.label}
                {model.available ? '' : ' (no API key)'}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}
