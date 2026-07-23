// ChatDetail — the layered execution view for one chat.
//
// The root is the owner's prompt. Below it, time flows down a fixed main-agent
// lane while helpers occupy concurrent lanes for their recorded lifetimes.
// Selecting a launch opens its prompt in a stable inspector; tool calls and raw
// execution logs stay out of the primary experience.

import React, { useState, useEffect, useRef, useLayoutEffect, useMemo } from 'react'
import { providerLabel, subStateMeta } from '../domain.js'
import { Markdown } from './Markdown.jsx'
import { Timeline } from './Timeline.jsx'

function fmtShortDate(ts) {
  if (ts == null) return ''
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ''
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return d.toLocaleDateString(undefined,
    sameYear ? { day: 'numeric', month: 'short' }
             : { day: 'numeric', month: 'short', year: 'numeric' })
}

function rootState(turns) {
  if (turns.some((turn) => turn.status === 'running')) return 'running'
  const last = turns[turns.length - 1]
  if (!last || last.status === 'done') return 'done'
  if (last.result === "couldn't complete") return 'failed'
  return 'stopped'
}

function timelineRootState(timeline, turns) {
  const agents = timeline && Array.isArray(timeline.agents) ? timeline.agents : []
  const runs = timeline && Array.isArray(timeline.main_runs) ? timeline.main_runs : []
  // The header describes where the chat is now. Historical failures and
  // interrupted runs remain in the timeline but must not override a newer run.
  const orderedRuns = [...runs].sort((a, b) => {
    const at = Date.parse(a && a.started_at || '')
    const bt = Date.parse(b && b.started_at || '')
    if (Number.isFinite(at) && Number.isFinite(bt) && at !== bt) return at - bt
    return String(a && a.id || '').localeCompare(String(b && b.id || ''))
  })
  const latestRun = orderedRuns[orderedRuns.length - 1]
  if (latestRun) {
    if (['running', 'resume_pending'].includes(latestRun.status)) return 'running'
    if (latestRun.status === 'failed') return 'failed'
    if (['stopped', 'interrupted', 'cancelled', 'canceled', 'parked', 'parked_notified'].includes(latestRun.status)) return 'stopped'
    if (latestRun.status === 'completed') {
      const currentAgents = agents.filter((agent) => agent.chat_run_id === latestRun.id)
      if (currentAgents.some((agent) => agent.state === 'running')) return 'running'
      if (currentAgents.some((agent) => agent.state === 'failed')) return 'failed'
      if (currentAgents.some((agent) => agent.state === 'stopped')) return 'stopped'
      if (currentAgents.length) return 'done'
      return rootState(turns)
    }
  }
  if (agents.some((agent) => agent.state === 'running')) return 'running'
  if (agents.some((agent) => agent.state === 'failed')) return 'failed'
  if (agents.some((agent) => agent.state === 'stopped')) return 'stopped'
  if (agents.length && agents.every((agent) => agent.state === 'done')) return 'done'
  return rootState(turns)
}

export function ChatDetail({ storage, chatId, chatMeta, viewStates, onBack, onOpenChat }) {
  const [detail, setDetail] = useState(undefined)
  const [rootPromptOpen, setRootPromptOpen] = useState(false)
  const scrollRef = useRef(null)
  const lastAppliedRef = useRef(-1)

  // Create the view state without mutating the shared Map during render. The
  // effect publishes a newly-created state after commit; event handlers then
  // update the stable object directly while this view is mounted.
  const store = useMemo(() => {
    const existing = viewStates && viewStates.get(chatId)
    if (existing) return { ...existing, prompts: existing.prompts || new Set() }
    return { scrollTop: 0, prompts: new Set() }
  }, [viewStates, chatId])
  useEffect(() => {
    if (viewStates) viewStates.set(chatId, store)
  }, [viewStates, chatId, store])

  useEffect(() => {
    lastAppliedRef.current = -1
    setRootPromptOpen(store.prompts.has('root'))
    setDetail(undefined)
    const unsub = storage.subscribe(`chats/${chatId}.json`, setDetail)
    return () => { try { unsub && unsub() } catch (_) { /* noop */ } }
  }, [storage, chatId, store])

  const title = (detail && detail.title) || (chatMeta && chatMeta.title) || 'Chat'
  const prompt = (detail && typeof detail.prompt_full === 'string') ? detail.prompt_full.trim() : ''
  const provider = (detail && detail.provider) || (chatMeta && chatMeta.provider)
  const turns = (detail && Array.isArray(detail.turns)) ? detail.turns : []
  const timeline = (detail && detail.timeline && typeof detail.timeline === 'object') ? detail.timeline : null
  const timelineAgents = timeline && Array.isArray(timeline.agents) ? timeline.agents : []
  const timelineEvents = timeline && Array.isArray(timeline.events) ? timeline.events : []
  const loaded = detail !== undefined
  const isEmpty = loaded && turns.length === 0 && timelineAgents.length === 0 && timelineEvents.length === 0
  const when = fmtShortDate((detail && detail.ts) || (chatMeta && chatMeta.ts))
  const state = subStateMeta(timelineRootState(timeline, turns))

  useLayoutEffect(() => {
    if (!loaded || isEmpty) return
    const el = scrollRef.current
    if (!el || !store.scrollTop) return
    if (lastAppliedRef.current >= 0 && Math.abs(el.scrollTop - lastAppliedRef.current) > 4) return
    const raf = requestAnimationFrame(() => {
      const target = Math.min(store.scrollTop, Math.max(0, el.scrollHeight - el.clientHeight))
      el.scrollTop = target
      lastAppliedRef.current = target
    })
    return () => cancelAnimationFrame(raf)
  }, [loaded, isEmpty, detail, store])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el || el.scrollTop === lastAppliedRef.current) return
    store.scrollTop = el.scrollTop
  }

  const onRootToggle = (event) => {
    const next = event.currentTarget.open
    setRootPromptOpen(next)
    if (next) store.prompts.add('root')
    else store.prompts.delete('root')
  }

  return (
    <div className="wf-root">
      <header className="wf-header">
        <button type="button" className="wf-back-text" onClick={onBack}>‹ Activity</button>
        <span className="wf-spacer" />
        <button type="button" className="wf-openchat" onClick={() => onOpenChat(chatId)}>
          Open chat ↗
        </button>
      </header>

      <main className="wf-scroll" ref={scrollRef} onScroll={onScroll} tabIndex={0} aria-label="Workflow timeline">
        {!loaded ? (
          <div className="wf-loading" role="status" aria-live="polite">
            <div className="wf-spinner" aria-hidden="true" />
            <span className="wf-sr-only">Loading workflow</span>
          </div>
        ) : isEmpty ? (
          <div className="wf-empty">
            <div className="wf-empty-mark" aria-hidden="true">✶</div>
            <div className="wf-empty-title">No recorded activity</div>
            <p className="wf-empty-text">This chat has no background work recorded yet.</p>
          </div>
        ) : (
          <div className="wf-flow">
            <section className="wf-root-task" aria-labelledby="wf-root-task-title">
              <span className="wf-root-node" aria-hidden="true" />
              <div className="wf-root-body">
                <div className="wf-flow-label">Main task</div>
                <h1 className="wf-chat-title" id="wf-root-task-title">{title}</h1>
                <div className="wf-chat-meta">
                  {provider && <span>{providerLabel(provider)}</span>}
                  {provider && when && <span className="wf-sep" aria-hidden="true" />}
                  {when && <span>{when}</span>}
                  <span className={`wf-sub-state ${state.cls}`}>{state.glyph} {state.label}</span>
                </div>
                {prompt && (
                  <details className="wf-prompt" open={rootPromptOpen} onToggle={onRootToggle}>
                    <summary className="wf-prompt-sum">
                      <span className="wf-cx" aria-hidden="true">›</span> Full prompt
                    </summary>
                    {rootPromptOpen && <div className="wf-prompt-body"><Markdown text={prompt} /></div>}
                  </details>
                )}
              </div>
            </section>
            <Timeline
              timeline={timeline}
              turns={turns}
              store={store}
              storage={storage}
              onOpenChat={() => onOpenChat(chatId)}
            />
          </div>
        )}
      </main>
    </div>
  )
}
