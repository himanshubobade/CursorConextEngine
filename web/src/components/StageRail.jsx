import React from 'react';

const LABELS = {
  intake: 'Describe',
  table_review: 'Confirm tables',
  plan_review: 'Review plan',
  code_review: 'Review code',
  publish_ready: 'Publish',
  deployed: 'Deploy',
};

const ORDER = ['intake', 'table_review', 'plan_review', 'code_review', 'publish_ready', 'deployed'];

export default function StageRail({ stage }) {
  const currentIndex = ORDER.indexOf(stage);
  return (
    <div className="stage-rail">
      {ORDER.map((key, index) => {
        const state = index < currentIndex ? 'done' : index === currentIndex ? 'current' : '';
        return (
          <span key={key} className={`stage-chip ${state}`}>
            {index < currentIndex ? '✓ ' : ''}
            {LABELS[key]}
          </span>
        );
      })}
    </div>
  );
}
