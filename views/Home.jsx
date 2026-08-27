// Home — the outcome journal. Each workflow is one tappable row showing the
// root task, concrete lifecycle state, latest outcome, and helper count. Owner
// input is a state on the relevant workflow, never a separate review inbox.
// Missing fields are omitted, never faked.

import React from 'react'
import {
  ArrowRotateCw,
  CheckCircleFilled,
  ChevronRight,
  CircleDashed,
  PauseCircle,
  Warning,
  XCircle,
} from '@openai/apps-sdk-ui/components/Icon'
import { statusDot, groupEntriesByDay } from '../domain.js'

function EntryCard({ entry, onOpen }) {
  const dot = statusDot(entry.status)
  const stateMeta = {
    done: { icon: CheckCircleFilled, label: 'Completed' },
    run: { icon: CircleDashed, label: 'Running' },
    wait: { icon: Warning, label: 'Waiting for you' },
    failed: { icon: XCircle, label: 'Failed' },
    stopped: { icon: PauseCircle, label: 'Stopped' },
    neutral: { icon: Warning, label: 'Status unavailable' },
  }[dot]
  const StateIcon = stateMeta.icon
  const stateLabel = stateMeta.label
  const tasks = Number.isFinite(entry.tasks) ? entry.tasks : null
  const reco = entry.recovered === true
  const headline = entry.title || entry.outcome || 'Untitled activity'
  const context = entry.outcome && entry.outcome.trim() !== headline.trim() ? entry.outcome : ''
  // Old stored indexes used `attention/not confirmed`. Until the first refresh
  // rewrites them, render those rows as ordinary completed history rather than
  // briefly reviving the retired review language.
  const result = entry.status === 'attention' ? 'completed' : entry.result
  return (
    <button type="button" className={`wf-entry${reco ? ' is-reco' : ''}`} onClick={() => onOpen(entry)}>
      <span className={`wf-entry-node ${dot}`} aria-hidden="true"><StateIcon /></span>
      <span className="wf-entry-copy">
        <span className="wf-sr-only">{stateLabel}. </span>
        <span className="wf-entry-title">{headline}</span>
        {context && <span className="wf-entry-context">{context}</span>}
        <span className="wf-entry-meta">
          {result && <span className="wf-result">{result}</span>}
          {reco && <span className="wf-pill is-reco">✦ recovered</span>}
          {tasks != null && (
            <span className="wf-tasks">{tasks} helper{tasks === 1 ? '' : 's'}</span>
          )}
        </span>
      </span>
      <ChevronRight className="wf-entry-go" aria-hidden="true" />
    </button>
  )
}

export function Home({
  appId, idx, loaded, online, refreshing, updatedLabel, onRefresh, onOpenDetail,
}) {
  const entries = (idx && Array.isArray(idx.entries)) ? idx.entries : []
  const groups = groupEntriesByDay(entries)
  const isEmpty = loaded && entries.length === 0
  const omittedChats = Math.max(0, Number(idx && idx.history && idx.history.chats_omitted) || 0)

  return (
    <div className="wf-root">
      <header className="wf-header">
        <div className="wf-header-inner">
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
            <span className="wf-subtitle">What your agents did</span>
          </div>
        </div>
        <div className="wf-header-actions">
          {refreshing && <span className="wf-sr-only" role="status">Checking for new workflow activity</span>}
          <button
            type="button"
            className={`wf-btn wf-btn-secondary wf-btn-icon${refreshing ? ' is-spinning' : ''}`}
            onClick={onRefresh}
            disabled={refreshing}
            title={refreshing ? 'Checking for new activity' : `Check for new activity · ${updatedLabel}`}
            aria-label="Check for new workflow activity"
          >
            <ArrowRotateCw aria-hidden="true" />
          </button>
        </div>
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
              When your assistant delegates work, you’ll see who ran, when,
              and how each attempt ended.
            </p>
            <div className="wf-empty-actions">
              <button type="button" className="wf-btn wf-btn-primary" onClick={onRefresh} disabled={refreshing}>
                {refreshing ? 'Checking…' : 'Refresh'}
              </button>
            </div>
          </div>
        ) : (
          <div className="wf-content">
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
