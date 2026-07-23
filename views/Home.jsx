// Home — the outcome JOURNAL. Reads like a diary of what the assistant got
// done: an app header, a "Needs your input" panel for actionable work, then the
// entries grouped by day. Each entry is one tappable row — root task, ambient
// status, latest outcome, and helper count — that opens the layered timeline.
// Missing fields are omitted, never faked.

import React, { useState } from 'react'
import { statusDot, groupEntriesByDay } from '../domain.js'

// The attention summary follows Reflection's skim-first contract: one useful
// reason is always visible, then the complete Reason / Next list expands in
// place. Each row opens the exact workflow instead of silently filtering the
// journal and leaving the owner to hunt for the problem.
function NeedsPanel({ items, open, onToggle, onOpen }) {
  const list = Array.isArray(items) ? items : []
  if (list.length === 0) return null
  const first = list[0]
  const n = list.length
  const kindLabel = (kind) => ({
    failed: 'Failed',
    stopped: 'Stopped',
    paused: 'Paused',
    unconfirmed: 'Unconfirmed',
  }[kind] || 'Needs review')
  return (
    <section className={`wf-needs-wrap${open ? ' is-open' : ''}`} aria-labelledby="wf-needs-title">
      <button
        type="button"
        className="wf-needs"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls="wf-needs-list"
      >
        <span className="wf-needs-ic" aria-hidden="true">!</span>
        <span className="wf-needs-tx">
          <span className="wf-needs-head" id="wf-needs-title">
            {n} workflow{n === 1 ? '' : 's'} need{n === 1 ? 's' : ''} your input
          </span>
          <span className="wf-needs-sub">
            {(first && first.reason) || 'Open to see what happened and what to do next'}
          </span>
        </span>
        <span className="wf-needs-go" aria-hidden="true">{open ? '⌃' : '⌄'}</span>
      </button>
      {open && (
        <div className="wf-needs-list" id="wf-needs-list">
          {list.map((item) => (
            <button
              type="button"
              className="wf-needs-item"
              key={item.chat_id}
              onClick={() => onOpen(item)}
            >
              <span className={`wf-needs-kind is-${item.kind || 'attention'}`}>
                {kindLabel(item.kind)}
              </span>
              <span className="wf-needs-item-copy">
                <span className="wf-needs-item-title">
                  {item.title || item.outcome || 'Workflow'}
                </span>
                <span className="wf-needs-reason">
                  {item.reason || 'This workflow has an unresolved result.'}
                </span>
                <span className="wf-needs-next">
                  <strong>Next:</strong> {item.next_action || 'Open the workflow to review what happened.'}
                </span>
              </span>
              <span className="wf-needs-item-go" aria-hidden="true">›</span>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

function EntryCard({ entry, onOpen }) {
  const dot = statusDot(entry.status)
  const glyph = dot === 'done' ? '✓' : dot === 'attn' ? '!' : dot === 'run' ? '◌' : '•'
  const stateLabel = dot === 'done'
    ? 'Done'
    : dot === 'attn'
      ? 'Needs input'
      : dot === 'run'
        ? 'Running'
        : 'Status unavailable'
  const tasks = Number.isFinite(entry.tasks) ? entry.tasks : null
  const reco = entry.recovered === true
  const headline = entry.title || entry.outcome || 'Untitled activity'
  const context = entry.outcome && entry.outcome.trim() !== headline.trim() ? entry.outcome : ''
  return (
    <button type="button" className={`wf-entry${reco ? ' is-reco' : ''}`} onClick={() => onOpen(entry)}>
      <span className={`wf-entry-node ${dot}`} aria-hidden="true">{glyph}</span>
      <span className="wf-entry-copy">
        <span className="wf-sr-only">{stateLabel}. </span>
        <span className="wf-entry-title">{headline}</span>
        {context && <span className="wf-entry-context">{context}</span>}
        <span className="wf-entry-meta">
          {entry.result && <span className="wf-result">{entry.result}</span>}
          {reco && <span className="wf-pill is-reco">✦ recovered</span>}
          {tasks != null && (
            <span className="wf-tasks">{tasks} helper{tasks === 1 ? '' : 's'}</span>
          )}
        </span>
      </span>
      <span className="wf-entry-go" aria-hidden="true">›</span>
    </button>
  )
}

export function Home({
  appId, idx, loaded, online, refreshing, updatedLabel, onRefresh, onOpenDetail,
}) {
  const entries = (idx && Array.isArray(idx.entries)) ? idx.entries : []
  const needs = (idx && Array.isArray(idx.needs_attention)) ? idx.needs_attention : []
  const [attentionOpen, setAttentionOpen] = useState(false)
  const groups = groupEntriesByDay(entries)
  const isEmpty = loaded && entries.length === 0
  const omittedChats = Math.max(0, Number(idx && idx.history && idx.history.chats_omitted) || 0)

  return (
    <div className="wf-root">
      <header className="wf-header">
        <div className="wf-brand">
          <img
            src={`/api/apps/${appId}/icon?size=64`}
            alt=""
            width={30}
            height={30}
            className="wf-brand-icon"
            onError={(e) => {
              e.currentTarget.style.display = 'none'
              const f = e.currentTarget.nextElementSibling
              if (f) f.style.display = 'flex'
            }}
          />
          <span className="wf-mark" style={{ display: 'none' }} aria-hidden="true">W</span>
          <div className="wf-heading">
            <h1 className="wf-title">Workflows</h1>
            <span className="wf-subtitle">What your assistant got done</span>
          </div>
        </div>
        <div className="wf-header-actions">
          {refreshing && <span className="wf-status-text" role="status">Updating…</span>}
          <button
            type="button"
            className={`wf-icon-btn${refreshing ? ' is-spinning' : ''}`}
            onClick={onRefresh}
            disabled={refreshing}
            title={updatedLabel}
            aria-label="Refresh"
          >
            <span className="wf-refresh-glyph" aria-hidden="true">⟳</span>
          </button>
        </div>
      </header>

      <main className="wf-scroll">
        {!loaded ? (
          <div className="wf-loading" role="status" aria-live="polite">
            <div className="wf-spinner" aria-hidden="true" />
            <span className="wf-sr-only">Loading activity</span>
          </div>
        ) : isEmpty ? (
          <div className="wf-empty">
            <div className="wf-empty-mark" aria-hidden="true">✶</div>
            <div className="wf-empty-title">Nothing here yet</div>
            <p className="wf-empty-text">
              When your assistant works on something in the background, it lands
              here as a plain-language journal — what it got done, and anything
              that needs your input.
            </p>
            <div className="wf-empty-actions">
              <button type="button" className="wf-btn wf-btn-primary" onClick={onRefresh} disabled={refreshing}>
                {refreshing ? 'Checking…' : 'Refresh'}
              </button>
            </div>
          </div>
        ) : (
          <div className="wf-content">
            <NeedsPanel
              items={needs}
              open={attentionOpen}
              onToggle={() => setAttentionOpen((value) => !value)}
              onOpen={onOpenDetail}
            />
            {omittedChats > 0 && (
              <p className="wf-history-note">
                Showing recent workflows · {omittedChats} older {omittedChats === 1 ? 'entry' : 'entries'} omitted
              </p>
            )}
            {groups.map((group) => {
              const groupReco = group.items.some((e) => e && e.recovered === true)
              return (
                <section className="wf-day-group" key={group.key} aria-labelledby={`wf-day-${group.key}`}>
                  <h2 className="wf-daylabel">
                    <span id={`wf-day-${group.key}`}>{group.label}</span>
                    {groupReco && <span className="wf-restored"> · ✦ restored</span>}
                  </h2>
                  <div className="wf-day-list">
                    {group.items.map((entry, i) => (
                      <EntryCard
                        key={entry.chat_id || `${group.key}:${i}`}
                        entry={entry}
                        onOpen={onOpenDetail}
                      />
                    ))}
                  </div>
                </section>
              )
            })}
          </div>
        )}
      </main>

      {!online && <div className="wf-sync-pill" role="status">Offline</div>}
    </div>
  )
}
