// Home — the outcome JOURNAL. Reads like a diary of what the assistant got
// done: an app header, a "Needs your input" panel for actionable work, then the
// entries grouped by day. Each entry is one tappable row — root task, ambient
// status, latest outcome, and helper count — that opens the layered timeline.
// Missing fields are omitted, never faked.

import React, { useState } from 'react'
import { statusDot, groupEntriesByDay } from '../domain.js'

const TRIAGE_META = {
  failed: {
    label: 'Failed',
    plural: 'failed',
    summary: 'The workflow could not complete.',
    action: 'Retry in chat',
    target: 'chat',
  },
  unconfirmed: {
    label: 'Unconfirmed',
    plural: 'unconfirmed',
    summary: 'A recorded check did not confirm the result.',
    action: 'Review trace',
    target: 'detail',
  },
  paused: {
    label: 'Waiting',
    plural: 'waiting',
    summary: 'The workflow may be waiting for your answer.',
    action: 'Resume in chat',
    target: 'chat',
  },
  stopped: {
    label: 'Incomplete',
    plural: 'incomplete',
    summary: 'Work stopped before completion was recorded.',
    action: 'Continue in chat',
    target: 'chat',
  },
  attention: {
    label: 'Review',
    plural: 'to review',
    summary: 'The workflow has an unresolved result.',
    action: 'Review trace',
    target: 'detail',
  },
}

function TriageCopy({ item, fallback }) {
  return (
    <span className="wf-needs-item-copy">
      <span className="wf-needs-item-title">
        {item.title || item.outcome || 'Workflow'}
      </span>
      <span className="wf-needs-item-reason">
        {item.reason || fallback}
      </span>
    </span>
  )
}

// A compact triage surface rather than a second journal. Shared explanations
// live at group level; each workflow row only needs its title and next verb.
function NeedsPanel({ items, open, onToggle, onOpen, onContinue }) {
  const list = Array.isArray(items) ? items : []
  if (list.length === 0) return null
  const n = list.length
  const groups = []
  const byKind = new Map()
  for (const item of list) {
    const kind = TRIAGE_META[item.kind] ? item.kind : 'attention'
    let group = byKind.get(kind)
    if (!group) {
      group = { kind, meta: TRIAGE_META[kind], items: [] }
      byKind.set(kind, group)
      groups.push(group)
    }
    group.items.push(item)
  }
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
            Review {n} workflow{n === 1 ? '' : 's'}
          </span>
          <span
            className="wf-needs-summary"
            aria-label={groups.map((group) => `${group.items.length} ${group.meta.plural}`).join(', ')}
          >
            {groups.map((group) => (
              <span className={`wf-needs-summary-chip is-${group.kind}`} key={group.kind}>
                {group.items.length} {group.meta.plural}
              </span>
            ))}
          </span>
        </span>
        <span className="wf-needs-go" aria-hidden="true">{open ? '⌃' : '⌄'}</span>
      </button>
      {open && (
        <div className="wf-needs-list" id="wf-needs-list">
          {groups.map((group) => (
            <section className="wf-needs-group" key={group.kind} aria-labelledby={`wf-needs-${group.kind}`}>
              <header className="wf-needs-group-head">
                <span className={`wf-needs-kind is-${group.kind}`} id={`wf-needs-${group.kind}`}>
                  {group.meta.label}
                </span>
                <span className="wf-needs-group-summary">{group.meta.summary}</span>
              </header>
              {group.items.map((item) => (
                <div
                  className="wf-needs-item"
                  key={item.chat_id}
                >
                  {group.meta.target === 'chat' ? (
                    <button
                      type="button"
                      className="wf-needs-item-main"
                      onClick={() => onOpen(item)}
                      aria-label={`Inspect trace: ${item.title || item.outcome || 'workflow'}`}
                    >
                      <TriageCopy item={item} fallback={group.meta.summary} />
                    </button>
                  ) : (
                    <div className="wf-needs-item-main">
                      <TriageCopy item={item} fallback={group.meta.summary} />
                    </div>
                  )}
                  <button
                    type="button"
                    className={`wf-needs-action is-${group.kind}`}
                    onClick={() => group.meta.target === 'chat' && onContinue
                      ? onContinue(item.chat_id)
                      : onOpen(item)}
                    aria-label={`${group.meta.action}: ${item.title || item.outcome || 'workflow'}`}
                  >
                    {group.meta.action}
                  </button>
                </div>
              ))}
            </section>
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
  appId, idx, loaded, online, refreshing, updatedLabel, onRefresh, onOpenDetail, onOpenChat,
}) {
  const entries = (idx && Array.isArray(idx.entries)) ? idx.entries : []
  const needs = (idx && Array.isArray(idx.needs_attention)) ? idx.needs_attention : []
  const [attentionOpen, setAttentionOpen] = useState(false)
  const attentionIds = new Set(needs
    .map((item) => item && item.chat_id)
    .filter(Boolean)
    .map(String))
  // Attention entries live in the triage surface only. Keeping them in the
  // journal as well made the same workflow appear twice, especially on mobile.
  const journalEntries = entries.filter((entry) => (
    !entry || !entry.chat_id || !attentionIds.has(String(entry.chat_id))
  ))
  const groups = groupEntriesByDay(journalEntries)
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
              onContinue={onOpenChat}
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
