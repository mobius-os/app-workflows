// Chronological workflow timeline. Time flows down; lane 0 is the main agent
// and concurrent helpers occupy reusable lanes. Connector SVG is decorative;
// the ordered event list remains the accessible source of truth.

import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  CheckCircleFilled,
  CircleDashed,
  PauseCircle,
  Warning,
  X,
  XCircle,
} from '@openai/apps-sdk-ui/components/Icon'
import { Markdown } from './Markdown.jsx'
import {
  TIMELINE_GEOMETRY, avatarFor, formatDuration, formatTimelineTime,
  layoutTimeline, subStateMeta,
} from '../domain.js'

function eventState(event, agent) {
  if (event.type === 'agent_terminal') return event.state
  if (event.type === 'agent_spawned' || event.type === 'agent_started') return 'running'
  return event.state || (agent && agent.state)
}

function stateMeta(value) {
  if (value === 'attention') return { cls: 'unknown', glyph: '?', label: 'check incomplete' }
  return subStateMeta(value)
}

const STATE_ICONS = {
  done: CheckCircleFilled,
  run: CircleDashed,
  failed: XCircle,
  stopped: PauseCircle,
  unknown: Warning,
}

export function WorkflowStateIcon({ state }) {
  const StateIcon = STATE_ICONS[state.cls] || Warning
  return <StateIcon className="wf-state-icon" aria-hidden="true" />
}

function eventLabel(event, agent, mainAgentId) {
  if (event.type === 'main_checkpoint') return event.summary || 'Main agent continued the task'
  if (event.subject_agent_id === mainAgentId) return event.summary || 'Main agent activity'
  const name = (agent && agent.name) || 'Helper'
  if (event.type === 'agent_spawned') return `${name} launched: ${(agent && agent.task_summary) || event.summary}`
  if (event.type === 'agent_started') return `${name} started: ${(agent && agent.task_summary) || event.summary}`
  if (event.type === 'agent_terminal') return `${name} ${subStateMeta(event.state).label}`
  return event.summary || `${name} activity`
}

function xForLane(lane) {
  return TIMELINE_GEOMETRY.laneOrigin + lane * TIMELINE_GEOMETRY.laneGap
}

function launchCohorts(model) {
  const byEvent = new Map()
  const byAgent = new Map()
  const groups = []
  let group = []
  const flush = () => {
    if (group.length > 1) {
      const cohort = {
        id: `cohort:${group[0].event_id}`,
        firstEventId: group[0].event_id,
        rows: [...group],
        agentIds: group.map((row) => row.subject_agent_id),
      }
      groups.push(cohort)
      group.forEach((row, index) => {
        const member = { ...cohort, position: index + 1, total: group.length }
        byEvent.set(row.event_id, member)
        byAgent.set(row.subject_agent_id, member)
      })
    }
    group = []
  }
  for (const row of model.rows) {
    const span = model.spansByAgent.get(row.subject_agent_id)
    // Cohorts represent one batch launch, not merely tasks that happened to
    // start close together. A checkpoint/terminal event closes the batch, and
    // retries for an existing logical helper stay inside that helper's detail.
    const isLaunch = span && span.startEvent === row
    if (!isLaunch) {
      flush()
      continue
    }
    const at = Date.parse(row.at || '')
    const previous = group[group.length - 1]
    const firstAt = group.length ? Date.parse(group[0].at || '') : NaN
    // Only assert a shared launch when the recorder gave us the launcher id,
    // and keep the whole cohort inside one 20-second window. Comparing with
    // only the previous launch could otherwise chain a long sequence together.
    const sameLauncher = previous && previous.actor_agent_id
      && previous.actor_agent_id === row.actor_agent_id
    if (previous && (!Number.isFinite(at) || !Number.isFinite(firstAt)
        || !sameLauncher || at - firstAt > 20_000)) {
      flush()
    }
    group.push(row)
  }
  flush()
  return { byEvent, byAgent, groups }
}

function isGenericAgentName(name) {
  return ['helper', 'explorer', 'codex', 'builder'].includes(String(name || '').trim().toLowerCase())
}

function stateCounts(agents) {
  const counts = { done: 0, running: 0, failed: 0, stopped: 0, unknown: 0 }
  for (const agent of agents) {
    const state = counts[agent.state] == null ? 'unknown' : agent.state
    counts[state] += 1
  }
  return counts
}

function compactStateSummary(counts) {
  return [
    counts.done && `${counts.done} done`,
    counts.running && `${counts.running} active`,
    counts.failed && `${counts.failed} failed`,
    counts.stopped && `${counts.stopped} stopped`,
    counts.unknown && `${counts.unknown} unknown`,
  ].filter(Boolean).join(' · ')
}

function CohortCard({ row, cohort, model, timeLabel, showTime, onExpand }) {
  const agents = cohort.agentIds
    .map((agentId) => model.agentsById.get(agentId))
    .filter(Boolean)
  const counts = stateCounts(agents)
  const summary = compactStateSummary(counts)
  return (
    <li
      className="wf-time-event is-agent_started is-cohort"
      style={{ '--wf-y': `${row.y}px`, '--wf-x': `${xForLane(row.lane)}px` }}
    >
      <time className={`wf-time-clock${showTime ? '' : ' is-repeat'}`} dateTime={row.at || undefined}>
        {showTime ? timeLabel : <span className="wf-sr-only">{timeLabel}</span>}
      </time>
      <div className="wf-time-event-body">
        <button
          type="button"
          className="wf-cohort-launch"
          onClick={onExpand}
          aria-label={`${agents.length} helpers launched. ${summary}. Show individual task cards.`}
        >
          <span className="wf-cohort-junction" aria-hidden="true">
            <i /><i /><i />
          </span>
            <span className="wf-cohort-copy">
              <strong>{agents.length} helpers launched</strong>
              <span>{summary || 'Status unavailable'}</span>
              <span className="wf-cohort-action">Show all tasks</span>
            </span>
        </button>
      </div>
    </li>
  )
}

function EventCard({ row, agent, parentAgent, mainAgentId, span, cohort, selected, onSelect, timeLabel, showTime }) {
  const isHelper = row.subject_agent_id !== mainAgentId
  const state = stateMeta(eventState(row, agent))
  const finalState = subStateMeta(agent && agent.state)
  const parent = agent && agent.parent_agent_id
  const unknownParent = isHelper && (!parent || agent.ancestry_quality === 'unknown')
  const startAt = span && span.startEvent && span.startEvent.at
  const rawDuration = row.type === 'agent_terminal' ? formatDuration(startAt, row.at) : ''
  const duration = rawDuration && span.startEvent.time_quality === 'exact' && row.time_quality === 'exact'
    ? rawDuration : rawDuration ? `~${rawDuration}` : ''
  const canSelect = isHelper && Boolean(agent)
  const summary = (agent && agent.task_summary) || row.summary || 'No task summary was recorded'

  let content
  if (row.type === 'main_checkpoint' || !isHelper) {
    content = (
      <div className="wf-time-main-card">
        {row.state && state.cls !== 'run' && (
          <span className={`wf-time-main-state ${state.cls}`}>
            <WorkflowStateIcon state={state} />
            {state.label}
          </span>
        )}
        <span className="wf-time-main-copy">{row.summary || 'Continued the task'}</span>
        {row.flag && <span className="wf-time-main-flag">{row.flag}</span>}
        {row.note && <span className="wf-time-main-note">{row.note}</span>}
      </div>
    )
  } else if (row.type === 'agent_spawned' || (row.type === 'agent_started' && span && span.startEvent === row)) {
    const av = avatarFor(agent && agent.kind)
    const recordedName = (agent && agent.name) || av.name
    const helperLabel = cohort
      ? `${isGenericAgentName(recordedName) ? 'Helper' : recordedName} ${cohort.position}/${cohort.total}`
      : isGenericAgentName(recordedName) ? '' : recordedName
    const inner = (
      <>
        <span className={`wf-junction-avatar ${av.cls}`} aria-hidden="true">{av.emoji}</span>
        <span className="wf-time-launch-copy">
          {helperLabel && <span className="wf-time-agent-name">{helperLabel}</span>}
          <span className={`wf-time-agent-task${unknownParent || (span && !span.authoritativeEnd) ? ' has-note' : ''}`}>{summary}</span>
          {agent && agent.attempt_count > 1 && (
            <span className="wf-time-parent-note is-attempts">{agent.attempt_count} attempts</span>
          )}
          {unknownParent && <span className="wf-time-parent-note">Parent not recorded</span>}
          {!unknownParent && parent && parent !== mainAgentId && (
            <span className="wf-time-parent-note is-parent">Launched by {(parentAgent && parentAgent.name) || 'another helper'}</span>
          )}
          {span && !span.authoritativeEnd && (
            <span className="wf-time-end-note">
              <WorkflowStateIcon state={finalState} />
              {finalState.label} · {agent && agent.state === 'running' ? 'no end yet' : 'end not recorded'}
            </span>
          )}
        </span>
      </>
    )
    content = canSelect ? (
      <button
        type="button"
        className={`wf-time-launch ${av.cls}${selected ? ' is-selected' : ''}`}
        onClick={onSelect}
        aria-expanded={selected}
        aria-controls="wf-agent-inspector"
        aria-label={`${eventLabel(row, agent, mainAgentId)}. Open details.`}
      >
        {inner}
      </button>
    ) : <div className={`wf-time-launch ${av.cls} is-static`}>{inner}</div>
  } else if (row.type === 'agent_started') {
    content = <span className="wf-time-small-event">Started</span>
  } else if (row.type === 'agent_terminal') {
    content = (
      <span className={`wf-time-terminal-label ${state.cls}`}>
        <WorkflowStateIcon state={state} />
        <span className="wf-sr-only">{state.label}{duration ? ', ' : ''}</span>
        {duration && <span>{duration}</span>}
      </span>
    )
  } else {
    content = <span className="wf-time-small-event">{row.summary || 'Activity'}</span>
  }

  return (
    <li
      className={`wf-time-event is-${row.type}`}
      style={{ '--wf-y': `${row.y}px`, '--wf-x': `${xForLane(row.lane)}px` }}
    >
      <time className={`wf-time-clock${showTime ? '' : ' is-repeat'}`} dateTime={row.at || undefined}>
        {showTime ? timeLabel : <span className="wf-sr-only">{timeLabel}</span>}
      </time>
      <div className="wf-time-event-body">
        {content}
      </div>
    </li>
  )
}

function TimelineDrawing({ model }) {
  const mainX = xForLane(0)
  const firstY = model.rows.length ? model.rows[0].y : 0
  const lastY = model.rows.length ? model.rows[model.rows.length - 1].y : firstY
  const mainStart = firstY - 18
  const mainEnd = lastY + 22
  const mainTravel = Math.max(1, mainEnd - mainStart)
  const mainPath = `M ${mainX} ${mainStart} C ${mainX + 4} ${mainStart + mainTravel * .3}, ${mainX - 4} ${mainEnd - mainTravel * .3}, ${mainX} ${mainEnd}`
  return (
    <svg
      className="wf-time-drawing"
      width={model.width}
      height={model.height}
      viewBox={`0 0 ${model.width} ${model.height}`}
      aria-hidden="true"
      focusable="false"
    >
      {model.rows.length > 0 && (
        <>
          <path className="wf-main-lifeline-under" d={mainPath} />
          <path className="wf-main-lifeline" d={mainPath} />
        </>
      )}
      {model.spans.map((span) => {
        const x = xForLane(span.lane)
        const parentId = span.agent.parent_agent_id
        const parentLane = parentId ? model.laneByAgent.get(parentId) : 0
        const parentX = xForLane(parentLane == null ? 0 : parentLane)
        const unknownParent = !parentId || span.agent.ancestry_quality === 'unknown'
        const ragY = span.endY
        const direction = x >= parentX ? 1 : -1
        const bend = Math.min(42, Math.abs(x - parentX) / 2)
        const lift = 10 + Math.min(8, Math.abs(x - parentX) / 18)
        const connector = `M ${parentX} ${span.startY} C ${parentX + direction * bend} ${span.startY - 2}, ${x - direction * bend} ${span.startY - lift}, ${x} ${span.startY}`
        const travel = Math.max(1, span.endY - span.startY)
        const sway = span.lane % 2 ? 4 : -4
        const lifeline = travel > 76
          ? `M ${x} ${span.startY} C ${x + sway} ${span.startY + travel * .32}, ${x - sway} ${span.endY - travel * .32}, ${x} ${span.endY}`
          : `M ${x} ${span.startY} V ${span.endY}`
        const terminalClass = span.authoritativeEnd ? subStateMeta(span.terminal.state).cls : ''
        return (
          <g key={span.agent.agent_id}>
            <path className="wf-connector-under" d={connector} />
            <path className={`wf-spawn-connector${unknownParent ? ' is-unknown' : ''}`} d={connector} />
            <path className="wf-agent-lifeline-under" d={lifeline} />
            <path className={`wf-agent-lifeline${span.authoritativeEnd ? '' : ' is-open'}`} d={lifeline} />
            <circle className="wf-agent-start-ring" cx={x} cy={span.startY} r="16" />
            {span.authoritativeEnd
              ? <>
                  <circle className={`wf-agent-end-ring ${terminalClass}`} cx={x} cy={span.endY} r="9" />
                  <circle className={`wf-agent-end-node ${terminalClass}`} cx={x} cy={span.endY} r="4.5" />
                </>
              : <path className="wf-agent-ragged" d={`M ${x - 5} ${ragY - 3} l 5 3 l 5 -3 M ${x - 5} ${ragY + 4} l 5 3 l 5 -3`} />}
          </g>
        )
      })}
      {model.rows.filter((row) => row.type === 'main_checkpoint').map((row) => (
        <circle key={row.event_id} className="wf-main-event-node" cx={mainX} cy={row.y} r="7" />
      ))}
    </svg>
  )
}

function Inspector({ agent, model, storage, onClose, onOpenChat }) {
  const [prompt, setPrompt] = useState(undefined)
  const headingRef = useRef(null)
  const dialogRef = useRef(null)
  const span = model.spansByAgent.get(agent.agent_id)
  const start = span && model.rows.find((row) => row.subject_agent_id === agent.agent_id
    && (row.type === 'agent_spawned' || row.type === 'agent_started'))
  const started = span && model.events.find((row) => row.subject_agent_id === agent.agent_id
    && row.type === 'agent_started')
  const end = span && span.terminal
  const state = subStateMeta(agent.state)
  const avatar = avatarFor(agent.kind)
  const parent = agent.parent_agent_id === model.mainAgentId
    ? 'Main agent'
    : agent.parent_agent_id
      ? ((model.agentsById.get(agent.parent_agent_id) || {}).name || 'Another helper')
      : 'Not recorded'
  const rawDuration = start && end ? formatDuration(start.at, end.at) : ''
  const duration = rawDuration && start.time_quality === 'exact' && end.time_quality === 'exact'
    ? rawDuration : rawDuration ? `~${rawDuration}` : ''

  useEffect(() => {
    let cancelled = false
    setPrompt(undefined)
    if (!storage || agent.prompt_available === false) { setPrompt(null); return undefined }
    storage.getJSON(`helpers/${agent.agent_id}.json`).then((doc) => {
      if (cancelled) return
      setPrompt(doc && typeof doc.brief_full === 'string' ? doc.brief_full.trim() : null)
    })
    return () => { cancelled = true }
  }, [agent.agent_id, agent.prompt_available, storage])

  useEffect(() => {
    const raf = requestAnimationFrame(() => headingRef.current && headingRef.current.focus())
    return () => cancelAnimationFrame(raf)
  }, [agent.agent_id])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return undefined
    const onKeyDown = (event) => {
      if (event.key !== 'Tab') return
      const focusable = [...dialog.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )].filter((element) => !element.hidden)
      if (!focusable.length) { event.preventDefault(); return }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === headingRef.current)) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && document.activeElement === headingRef.current) {
        event.preventDefault(); first.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus()
      }
    }
    dialog.addEventListener('keydown', onKeyDown)
    return () => dialog.removeEventListener('keydown', onKeyDown)
  }, [agent.agent_id, prompt])

  return (
    <div
      ref={dialogRef}
      className="wf-agent-inspector"
      id="wf-agent-inspector"
      role="dialog"
      aria-modal="true"
      aria-labelledby="wf-inspector-title"
    >
      <header className="wf-inspector-head">
        <span className={`wf-inspector-avatar ${avatar.cls}`} aria-hidden="true">{avatar.emoji}</span>
        <div>
          <div className="wf-flow-label">Assignment</div>
          <h2 id="wf-inspector-title" ref={headingRef} tabIndex={-1}>{agent.name || 'Helper'}</h2>
        </div>
        <button type="button" className="wf-inspector-close" onClick={onClose} aria-label="Close helper details">
          <X width="1em" height="1em" aria-hidden="true" />
        </button>
      </header>
      <div className="wf-inspector-scroll">
        <p className="wf-inspector-task">{agent.task_summary}</p>
        <dl className="wf-inspector-facts">
          <div><dt>Status</dt><dd><span className={`wf-sub-state ${state.cls}`}><WorkflowStateIcon state={state} /> {state.label}</span></dd></div>
          {agent.attempt_count > 1 && (
            <div>
              <dt>Attempts</dt>
              <dd>
                {agent.attempt_count} ({Object.entries(agent.attempt_states || {})
                  .filter(([, count]) => count > 0)
                  .map(([attemptState, count]) => `${count} ${attemptState}`)
                  .join(', ')})
              </dd>
            </div>
          )}
          <div><dt>Launched by</dt><dd>{parent}</dd></div>
          {start && <div><dt>{start.type === 'agent_spawned' ? 'Launched' : 'Started'}</dt><dd><time dateTime={start.at || undefined}>{formatTimelineTime(start.at, start.time_quality)}</time></dd></div>}
          {started && started.event_id !== (start && start.event_id) && (
            <div><dt>Started</dt><dd><time dateTime={started.at || undefined}>{formatTimelineTime(started.at, started.time_quality)}</time></dd></div>
          )}
          {end && <div><dt>Finished</dt><dd><time dateTime={end.at || undefined}>{formatTimelineTime(end.at, end.time_quality)}</time></dd></div>}
          {duration && <div><dt>Duration</dt><dd>{duration}</dd></div>}
          {!end && agent.state !== 'running' && <div><dt>Finished</dt><dd>End time not recorded</dd></div>}
          {agent.timing_conflict && <div><dt>Timing</dt><dd>Recorded times conflict</dd></div>}
        </dl>
        {agent.outcome_summary && <section className="wf-inspector-section"><h3>Outcome</h3><p>{agent.outcome_summary}</p></section>}
        {agent.state !== 'done' && (
          <section className="wf-inspector-section wf-inspector-action">
            <h3>What you can do</h3>
            <p>
              {agent.state === 'failed'
                ? 'Review the prompt and outcome above, then retry this work from the original chat.'
                : agent.state === 'running'
                  ? 'The helper is still active. Open the original chat if you need to steer or stop it.'
                  : 'The trace has no confirmed completion. Check the original chat, then continue the work if it is still needed.'}
            </p>
            <button type="button" className="wf-btn wf-btn-primary" onClick={onOpenChat}>
              Open original chat ↗
            </button>
          </section>
        )}
        <section className="wf-inspector-section">
          <h3>Full prompt</h3>
          {prompt === undefined
            ? <div className="wf-prompt-loading" role="status">Loading full prompt…</div>
            : prompt
              ? <Markdown text={prompt} />
              : <div className="wf-prompt-loading">Prompt unavailable.</div>}
        </section>
      </div>
    </div>
  )
}

export function Timeline({ timeline, turns, store, storage, onOpenChat }) {
  const model = useMemo(() => layoutTimeline(timeline, turns), [timeline, turns])
  const [selectedId, setSelectedId] = useState(() => store && store.selectedAgentId || null)
  // Individual assignment bubbles are the primary representation: they answer
  // "what is each helper doing?" without another click. Dense launch grouping
  // remains available as an explicit owner choice.
  const [cohortsExpanded, setCohortsExpanded] = useState(true)
  const triggerRef = useRef(null)
  const timelineRef = useRef(null)
  const selected = selectedId && model.agentsById.get(selectedId)
  const omittedAgents = model.retention.agents_omitted
  const omittedEvents = model.retention.events_omitted
  const counts = useMemo(() => stateCounts(model.agents), [model.agents])
  const incompleteCount = counts.failed + counts.stopped + counts.unknown
  const cohorts = useMemo(() => launchCohorts(model), [model])
  const healthSegments = [
    { key: 'done', label: 'done', count: counts.done },
    { key: 'running', label: 'active', count: counts.running },
    { key: 'failed', label: 'failed', count: counts.failed },
    { key: 'stopped', label: 'stopped', count: counts.stopped },
    { key: 'unknown', label: 'unknown', count: counts.unknown },
  ].filter((segment) => segment.count > 0)
  const displayRows = useMemo(() => {
    const visibleRows = cohortsExpanded ? model.rows : model.rows.filter((row) => {
      const cohort = cohorts.byAgent.get(row.subject_agent_id)
      return !cohort || row.event_id === cohort.firstEventId
    })
    let previousTime = null
    return visibleRows.map((row) => {
      const timeLabel = row.at ? formatTimelineTime(row.at, row.time_quality) : 'Time unavailable'
      const showTime = timeLabel === 'Time unavailable' || timeLabel !== previousTime
      previousTime = timeLabel
      return { row, timeLabel, showTime }
    })
  }, [model.rows, cohorts, cohortsExpanded])

  useEffect(() => {
    if (selectedId && !model.agentsById.has(selectedId)) {
      setSelectedId(null)
      if (store) store.selectedAgentId = null
      triggerRef.current = null
      requestAnimationFrame(() => timelineRef.current && timelineRef.current.focus())
    }
  }, [model, selectedId])

  useEffect(() => {
    if (!selected) return undefined
    const onKeyDown = (event) => { if (event.key === 'Escape') closeInspector() }
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
    }
  }, [selected])

  const selectAgent = (agentId, trigger) => {
    triggerRef.current = trigger
    setSelectedId(agentId)
    if (store) store.selectedAgentId = agentId
  }
  const closeInspector = () => {
    setSelectedId(null)
    if (store) store.selectedAgentId = null
    const trigger = triggerRef.current
    const destination = trigger && trigger.isConnected ? trigger : timelineRef.current
    requestAnimationFrame(() => destination && destination.focus())
  }

  if (!model.rows.length) return null
  return (
    <>
      <section ref={timelineRef} className="wf-time-section" aria-label="Chronological agent timeline" tabIndex={-1}>
        <div className="wf-time-overview">
          <div className="wf-time-overview-copy">
            <span className="wf-time-overview-title">
              {model.agents.length
                ? `${model.agents.length} helper${model.agents.length === 1 ? '' : 's'}`
                : 'Main agent only'}
            </span>
            <span className="wf-time-overview-count">
              {counts.done} done
              {counts.running ? ` · ${counts.running} active` : ''}
              {incompleteCount ? ` · ${incompleteCount} incomplete` : ''}
            </span>
          </div>
          {model.agents.length > 0 && (
            <div
              className="wf-time-health"
              role="img"
              aria-label={healthSegments.map((segment) => `${segment.count} ${segment.label}`).join(', ')}
            >
              {healthSegments.map((segment) => (
                <span
                  className={`is-${segment.key}`}
                  key={segment.key}
                  style={{ flexGrow: segment.count, flexBasis: 0 }}
                />
              ))}
            </div>
          )}
          {model.agents.length > 0 && (
            <div className="wf-time-facts" aria-label="Workflow summary">
              {healthSegments.map((segment) => (
                <span className={`is-${segment.key}`} key={segment.key}>
                  <i aria-hidden="true" />{segment.count} {segment.label}
                </span>
              ))}
              <span>{model.maxLane} at once</span>
              {cohorts.groups.length > 0 && (
                <button
                  type="button"
                  className="wf-cohort-toggle"
                  onClick={() => setCohortsExpanded((value) => !value)}
                >
                  {cohortsExpanded ? 'Group launches' : 'Show every task'}
                </button>
              )}
            </div>
          )}
          {(omittedAgents > 0 || omittedEvents > 0) && (
            <div className="wf-time-retention">
              {omittedAgents > 0 && `${omittedAgents} lower-detail helper record${omittedAgents === 1 ? '' : 's'} summarized`}
              {omittedAgents > 0 && omittedEvents > 0 && ' · '}
              {omittedEvents > 0 && `${omittedEvents} lower-level event${omittedEvents === 1 ? '' : 's'} omitted`}
            </div>
          )}
        </div>
        <div className="wf-time-scroll" tabIndex={model.maxLane > 0 ? 0 : undefined} aria-label={model.maxLane > 0 ? 'Scrollable workflow lanes' : undefined}>
          <div className="wf-time-canvas" style={{ width: `${model.width}px`, height: `${model.height}px` }}>
            <TimelineDrawing model={model} />
            <ol className="wf-time-events">
              {displayRows.map(({ row, timeLabel, showTime }) => {
                const cohort = cohorts.byEvent.get(row.event_id)
                if (!cohortsExpanded && cohort) {
                  return (
                    <CohortCard
                      key={cohort.id}
                      row={row}
                      cohort={cohort}
                      model={model}
                      timeLabel={timeLabel}
                      showTime={showTime}
                      onExpand={() => setCohortsExpanded(true)}
                    />
                  )
                }
                const agent = model.agentsById.get(row.subject_agent_id)
                const parentAgent = agent && agent.parent_agent_id
                  ? model.agentsById.get(agent.parent_agent_id) : null
                const span = model.spansByAgent.get(row.subject_agent_id)
                return (
                  <EventCard
                    key={row.event_id}
                    row={row}
                    agent={agent}
                    parentAgent={parentAgent}
                    mainAgentId={model.mainAgentId}
                    span={span}
                    cohort={cohorts.byEvent.get(row.event_id)}
                    selected={selectedId === row.subject_agent_id}
                    onSelect={(event) => selectAgent(row.subject_agent_id, event.currentTarget)}
                    timeLabel={timeLabel}
                    showTime={showTime}
                  />
                )
              })}
            </ol>
          </div>
        </div>
      </section>
      {selected && (
        <>
          <button type="button" className="wf-inspector-backdrop" onClick={closeInspector} tabIndex={-1} aria-hidden="true" />
          <Inspector
            agent={selected}
            model={model}
            storage={storage}
            onClose={closeInspector}
            onOpenChat={onOpenChat}
          />
        </>
      )}
    </>
  )
}
