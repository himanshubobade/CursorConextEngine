import React, { useEffect, useRef, useState } from 'react';
import Markdown from './Markdown.jsx';
import StageRail from './StageRail.jsx';

const PLACEHOLDERS = {
  intake:
    'Describe what you need. For example: "Monthly revenue by product for the last 12 months, excluding cancelled orders."',
  table_review: 'Tell me what is missing or wrong about these tables, and I will search again.',
  plan_review: 'Ask for a change to the plan - a different join, filter, grain or write mode.',
  code_review: 'Ask for a change to the code and I will regenerate and revalidate it.',
  publish_ready: 'Ask for a change and I will regenerate the code.',
  deployed: 'Ask for a change and I will regenerate the code.',
};

export default function ChatPanel({
  session,
  messages,
  statusLine,
  busy,
  error,
  onSend,
  header,
}) {
  const [draft, setDraft] = useState('');
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, statusLine]);

  const submit = () => {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft('');
    onSend(text);
  };

  const onKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className="center">
      <div className="pane-header">{header}</div>
      <StageRail stage={session?.stage || 'intake'} />

      <div className="pane-body">
        {messages.length === 0 && !busy && (
          <div className="empty">
            Describe the data you need in business terms.
            <br />
            I will find the tables, plan the pipeline, and write the project.
          </div>
        )}

        <div className="messages">
          {messages.map((message) => (
            <div key={message.id} className={`msg ${message.role}`}>
              <div className="avatar">{message.role === 'user' ? 'BA' : 'CE'}</div>
              <div className="body">
                <Markdown text={message.content} />
              </div>
            </div>
          ))}
        </div>

        {statusLine && (
          <div className="status-line" style={{ marginTop: 14 }}>
            <span className="spinner" />
            {statusLine}
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="pane-footer">
        {error && <div className="error-banner">{error}</div>}
        <div className="composer">
          <textarea
            value={draft}
            placeholder={PLACEHOLDERS[session?.stage] || PLACEHOLDERS.intake}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={busy}
          />
          <div className="composer-row">
            <span className="small grow">Enter to send, Shift+Enter for a new line</span>
            <button className="primary" onClick={submit} disabled={busy || !draft.trim()}>
              {busy ? 'Working…' : 'Send'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
