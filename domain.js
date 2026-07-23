// Pure display helpers for Workflows: day-grouping, owner-facing status/avatar
// mapping, and formatting. No React, no I/O — this is the testable core.
//
// Product-truth rules baked in here: status is DERIVED from the stored
// artifacts (never model-generated); a missing value is OMITTED by the view,
// never shown as a zero or a fake placeholder. The journal reads as a diary of
// outcomes, so the labels here are plain owner language, not machine vocabulary.

// ---------------------------------------------------------------------------
// Provider chip
// ---------------------------------------------------------------------------

export function providerLabel(provider) {
  if (provider === 'claude') return 'Claude'
  if (provider === 'codex') return 'Codex'
  return (typeof provider === 'string' && provider ? provider : 'Agent')
}

// ---------------------------------------------------------------------------
// Ambient status — the three journal/turn states and their dot styling.
// `done` is the quiet default; `attention` is the amber "needs input"; `run`
// is in-progress. Anything unknown stays neutral rather than inventing success.
// ---------------------------------------------------------------------------

export function statusDot(status) {
  if (status === 'done') return 'done'
  if (status === 'attention') return 'attn'
  if (status === 'running') return 'run'
  return 'neutral'
}

// ---------------------------------------------------------------------------
// Subagent identity — kind → avatar glyph, wrapper class, default name and the
// plain one-line role. The stored `name` on a sub wins when present; this only
// fills the glyph/class and a fallback name.
// ---------------------------------------------------------------------------

const AVATARS = {
  explore: { cls: 'explore', emoji: '🔍', name: 'Explorer' },
  codex: { cls: 'codex', emoji: '◆', name: 'Codex' },
  build: { cls: 'build', emoji: '🛠', name: 'Builder' },
  general: { cls: 'build', emoji: '🛠', name: 'Helper' },
}

export function avatarFor(kind) {
  return AVATARS[kind] || AVATARS.general
}

// A subagent's fate, shown as a small badge on its card. Only `done` and `run`
// appear in the common path; `failed`/`stopped` are styled too so a drifted
// state never renders unlabelled.
export function subStateMeta(state) {
  if (state === 'done') return { cls: 'done', glyph: '✓', label: 'done' }
  if (state === 'running') return { cls: 'run', glyph: '◌', label: 'running' }
  if (state === 'failed') return { cls: 'failed', glyph: '✕', label: 'failed' }
  if (state === 'stopped') return { cls: 'stopped', glyph: '‖', label: 'stopped' }
  return { cls: 'unknown', glyph: '?', label: 'status unavailable' }
}

// ---------------------------------------------------------------------------
// Chronological helper timeline
// ---------------------------------------------------------------------------

export const TIMELINE_GEOMETRY = Object.freeze({
  top: 28,
  row: 72,
  laneOrigin: 58,
  laneGap: 110,
  cardOffset: 15,
  cardWidth: 108,
  mainCardWidth: 132,
  rightPad: 16,
  bottom: 38,
})

function isoMs(value) {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

const TIMELINE_CLOCK = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit', minute: '2-digit',
})

function canonicalAgentState(value) {
  if (value === 'completed' || value === 'complete' || value === 'finished') return 'done'
  if (value === 'cancelled' || value === 'canceled' || value === 'interrupted') return 'stopped'
  if (value === 'working' || value === 'in_progress') return 'running'
  return ['done', 'failed', 'stopped', 'running'].includes(value) ? value : 'unknown'
}

function eventType(raw) {
  const type = String(raw.type || raw.kind || '')
  if (type === 'agent_completed') return ['agent_terminal', 'done']
  if (type === 'agent_failed') return ['agent_terminal', 'failed']
  if (['agent_stopped', 'agent_interrupted', 'agent_cancelled'].includes(type)) {
    return ['agent_terminal', 'stopped']
  }
  return [type, canonicalAgentState(raw.state)]
}

function normalizeEvent(raw, index) {
  const [type, mappedState] = eventType(raw || {})
  const occurredAt = raw.occurred_at || raw.at || null
  const observedAt = raw.observed_at || occurredAt || null
  const quality = occurredAt
    ? (raw.time_quality || 'exact')
    : observedAt ? 'observed' : 'unknown'
  const order = Number.isFinite(Number(raw.order ?? raw.sequence ?? raw.id))
    ? Number(raw.order ?? raw.sequence ?? raw.id)
    : index
  return {
    event_id: String(raw.event_id || raw.id || `event-${index}`),
    order,
    type,
    occurred_at: occurredAt,
    observed_at: observedAt,
    at: occurredAt || observedAt,
    time_quality: quality,
    actor_agent_id: raw.actor_agent_id || raw.parent_agent_id || null,
    subject_agent_id: raw.subject_agent_id || raw.agent_id || null,
    state: raw.state === 'attention' ? 'attention' : mappedState,
    summary: raw.summary || raw.outcome || '',
    flag: raw.flag || '',
    _source_index: index,
  }
}

function compareEvents(a, b) {
  if (a.order !== b.order) return a.order - b.order
  const am = isoMs(a.at)
  const bm = isoMs(b.at)
  if (am !== bm && am != null && bm != null) return am - bm
  return a._source_index - b._source_index || a.event_id.localeCompare(b.event_id)
}

function v3Timeline(turns, mainAgentId) {
  const agents = []
  const events = []
  let order = 0
  for (const [turnIndex, turn] of (Array.isArray(turns) ? turns : []).entries()) {
    const checkpointAt = turn && turn.ts
    events.push(normalizeEvent({
      event_id: `v3-main-${turnIndex}`,
      order: order++,
      type: 'main_checkpoint',
      occurred_at: checkpointAt,
      time_quality: checkpointAt ? 'observed' : 'unknown',
      actor_agent_id: mainAgentId,
      subject_agent_id: mainAgentId,
      state: turn && turn.status,
      summary: (turn && turn.outcome) || 'Continued the task',
      flag: (turn && turn.flag) || '',
    }, events.length))
    for (const [subIndex, sub] of (Array.isArray(turn && turn.subs) ? turn.subs : []).entries()) {
      const id = String(sub.agent_id || `v3-${turnIndex}-${subIndex}`)
      agents.push({
        agent_id: id,
        parent_agent_id: null,
        provider: sub.provider || '',
        kind: sub.kind || 'general',
        name: sub.name || '',
        task_summary: sub.ask || 'No task summary was recorded',
        state: canonicalAgentState(sub.state),
        prompt_available: sub.prompt_available !== false,
        outcome_summary: sub.outcome || '',
        attempt_count: 1,
        attempt_states: { [canonicalAgentState(sub.state)]: 1 },
        last_activity_at: null,
        depth: Math.max(1, Number(sub.depth) || 1),
        ancestry_quality: 'unknown',
        legacy: true,
      })
      events.push(normalizeEvent({
        event_id: `v3-spawn-${turnIndex}-${subIndex}`,
        order: order++,
        type: 'agent_spawned',
        observed_at: checkpointAt,
        time_quality: checkpointAt ? 'observed' : 'unknown',
        actor_agent_id: mainAgentId,
        subject_agent_id: id,
        summary: sub.ask || '',
      }, events.length))
    }
  }
  return {
    mainAgentId, agents, events,
    retention: { agents_omitted: 0, events_omitted: 0 },
  }
}

// Normalizes schema v4 while retaining a deliberately conservative schema-v3
// fallback. V3 status is shown, but it never becomes a fabricated terminal
// event: its lifetime therefore ends with the dashed "end not recorded" mark.
export function normalizeTimeline(timeline, turns = []) {
  if (!timeline || !Array.isArray(timeline.agents) || !Array.isArray(timeline.events)) {
    return v3Timeline(turns, 'main')
  }
  const mainAgentId = String(timeline.main_agent_id || 'main')
  const agents = timeline.agents.map((raw, index) => ({
    agent_id: String(raw.agent_id || raw.id || `agent-${index}`),
    chat_run_id: raw.chat_run_id == null ? null : String(raw.chat_run_id),
    parent_agent_id: raw.parent_agent_id == null ? null : String(raw.parent_agent_id),
    provider: raw.provider || '',
    kind: raw.kind || raw.agent_type || 'general',
    name: raw.name || '',
    task_summary: raw.task_summary || raw.ask || raw.summary || 'No task summary was recorded',
    state: canonicalAgentState(raw.state),
    prompt_available: raw.prompt_available !== false,
    outcome_summary: raw.outcome_summary || raw.outcome || '',
    attempt_count: Math.max(1, Number(raw.attempt_count) || 1),
    attempt_states: raw.attempt_states && typeof raw.attempt_states === 'object'
      ? Object.fromEntries(Object.entries(raw.attempt_states)
        .filter(([, count]) => Number.isFinite(Number(count)) && Number(count) > 0)
        .map(([state, count]) => [canonicalAgentState(state), Number(count)]))
      : {},
    last_activity_at: raw.last_activity_at || null,
    depth: Math.max(1, Number(raw.depth) || 1),
    ancestry_quality: raw.ancestry_quality || (raw.parent_agent_id ? 'exact' : 'unknown'),
    timing_conflict: raw.timing_conflict === true,
    legacy: false,
  }))
  const events = timeline.events.map(normalizeEvent).filter((event) => event.type).sort(compareEvents)
  const agentIndex = new Map(agents.map((agent) => [agent.agent_id, agent]))
  const eventsByAgent = new Map()
  for (const event of events) {
    if (!event.subject_agent_id) continue
    const list = eventsByAgent.get(event.subject_agent_id) || []
    list.push(event)
    eventsByAgent.set(event.subject_agent_id, list)
  }
  for (const agent of agents) {
    const ownEvents = eventsByAgent.get(agent.agent_id) || []
    const launch = ownEvents.find((event) => event.type === 'agent_spawned')
    if (!agent.parent_agent_id && launch && launch.actor_agent_id) {
      agent.parent_agent_id = String(launch.actor_agent_id)
      agent.ancestry_quality = 'exact'
    }
    const terminal = [...ownEvents].reverse().find((event) => event.type === 'agent_terminal')
    if (terminal && terminal.state !== 'unknown') agent.state = terminal.state
  }
  // Compute mobile indentation from the real parent chain. Stored depth is a
  // fallback only; exact ancestry wins and cycles stop without guessing.
  for (const agent of agents) {
    let depth = 1
    let parent = agent.parent_agent_id
    const seen = new Set([agent.agent_id])
    while (parent && parent !== mainAgentId && agentIndex.has(parent) && !seen.has(parent)) {
      seen.add(parent)
      depth += 1
      parent = agentIndex.get(parent).parent_agent_id
    }
    if (depth > 1 || agent.parent_agent_id === mainAgentId) agent.depth = depth
  }

  // Main checkpoints are still the most useful skim layer. During migration,
  // retain them from v3 turns without using their helper cards a second time.
  if (!events.some((event) => event.type === 'main_checkpoint')) {
    const checkpoints = turns.map((turn, index) => normalizeEvent({
      event_id: `main-${index}`,
      order: index,
      type: 'main_checkpoint',
      occurred_at: turn.ts,
      time_quality: turn.ts ? 'observed' : 'unknown',
      actor_agent_id: mainAgentId,
      subject_agent_id: mainAgentId,
      state: turn.status,
      summary: turn.outcome || 'Continued the task',
      flag: turn.flag || '',
    }, index)).sort((a, b) => {
      const am = isoMs(a.at)
      const bm = isoMs(b.at)
      if (am != null && bm != null && am !== bm) return am - bm
      return a._source_index - b._source_index
    })
    // Preserve the parser's causal event order. Checkpoints are annotations,
    // so insert each before the first later lifecycle time rather than sorting
    // the authoritative events again by potentially skewed clocks.
    const merged = []
    let checkpointIndex = 0
    for (const event of events) {
      const eventMs = isoMs(event.at)
      while (checkpointIndex < checkpoints.length) {
        const checkpointMs = isoMs(checkpoints[checkpointIndex].at)
        if (checkpointMs == null || eventMs == null || checkpointMs > eventMs) break
        merged.push(checkpoints[checkpointIndex++])
      }
      merged.push(event)
    }
    merged.push(...checkpoints.slice(checkpointIndex))
    events.splice(0, events.length, ...merged)
  }
  const rawRetention = timeline.retention && typeof timeline.retention === 'object'
    ? timeline.retention : {}
  return {
    mainAgentId,
    mainRuns: Array.isArray(timeline.main_runs) ? timeline.main_runs : [],
    agents,
    events,
    retention: {
      agents_omitted: Math.max(0, Number(rawRetention.agents_omitted) || 0),
      events_omitted: Math.max(0, Number(rawRetention.events_omitted) || 0),
    },
  }
}

// Deterministic interval coloring: main is lane 0; helpers receive the lowest
// free positive lane, never move while alive, and release only at an explicit
// terminal event. Vertical spacing communicates causal order, not elapsed
// time; the minute labels carry time without making long waits consume space.
// Width follows peak concurrency rather than total work.
export function layoutTimeline(timeline, turns = []) {
  const normalized = normalizeTimeline(timeline, turns)
  // A launch followed seconds later by a provider "started" acknowledgement
  // is useful evidence, but showing both as separate timeline steps adds noise.
  // Keep start-only histories visible while folding the common spawn→start pair
  // into one launch step; the exact started_at remains available in details.
  const launched = new Set()
  const displayEvents = []
  for (const event of normalized.events) {
    if (event.type === 'agent_spawned' && event.subject_agent_id) {
      launched.add(event.subject_agent_id)
    }
    if (event.type === 'agent_started' && launched.has(event.subject_agent_id)) continue
    displayEvents.push(event)
  }
  const agentsById = new Map(normalized.agents.map((agent) => [agent.agent_id, agent]))
  const laneByAgent = new Map([[normalized.mainAgentId, 0]])
  const activeByLane = new Map()
  const rows = []
  let y = TIMELINE_GEOMETRY.top
  let maxLane = 0

  for (const [index, event] of displayEvents.entries()) {
    if (index) y += TIMELINE_GEOMETRY.row
    // V3 has no terminal timestamps. Once its observed timestamp group has
    // passed, recycle slots for helpers whose stored status is already final;
    // their visual span remains short and ragged, never a fabricated finish.
    if (index && displayEvents[index - 1].at !== event.at) {
      for (const [lane, activeId] of activeByLane.entries()) {
        const active = agentsById.get(activeId)
        if (active && active.legacy && active.state !== 'running') activeByLane.delete(lane)
      }
    }
    const id = event.subject_agent_id
    if (event.type === 'agent_terminal') {
      if (!laneByAgent.has(id) && id !== normalized.mainAgentId) {
        let lane = 1
        while (activeByLane.has(lane)) lane += 1
        laneByAgent.set(id, lane)
        activeByLane.set(lane, id)
        maxLane = Math.max(maxLane, lane)
      }
      const lane = laneByAgent.get(id) ?? 0
      rows.push({ ...event, lane, y })
      if (lane > 0) activeByLane.delete(lane)
      continue
    }
    if ((event.type === 'agent_spawned' || event.type === 'agent_started') && id !== normalized.mainAgentId) {
      if (!laneByAgent.has(id)) {
        let lane = 1
        while (activeByLane.has(lane)) lane += 1
        laneByAgent.set(id, lane)
        activeByLane.set(lane, id)
        maxLane = Math.max(maxLane, lane)
      }
    }
    rows.push({ ...event, lane: laneByAgent.get(id) ?? 0, y })
  }

  const lastY = rows.length ? rows[rows.length - 1].y : TIMELINE_GEOMETRY.top
  const terminalByAgent = new Map(rows.filter((row) => row.type === 'agent_terminal')
    .map((row) => [row.subject_agent_id, row]))
  const firstByAgent = new Map()
  for (const row of rows) {
    if (row.subject_agent_id && !firstByAgent.has(row.subject_agent_id)
        && (row.type === 'agent_spawned' || row.type === 'agent_started')) {
      firstByAgent.set(row.subject_agent_id, row)
    }
  }
  const spans = normalized.agents.map((agent) => {
    const start = firstByAgent.get(agent.agent_id)
    if (!start) return null
    const terminal = terminalByAgent.get(agent.agent_id)
    const openEnd = agent.state === 'running'
      ? Math.max(start.y + 48, lastY + 32)
      : start.y + 52
    return {
      agent,
      lane: laneByAgent.get(agent.agent_id) || 1,
      startY: start.y,
      endY: terminal ? terminal.y : openEnd,
      terminal: terminal || null,
      authoritativeEnd: Boolean(terminal),
      startEvent: start,
    }
  }).filter(Boolean)
  const spansByAgent = new Map(spans.map((span) => [span.agent.agent_id, span]))

  const height = Math.max(150, lastY + TIMELINE_GEOMETRY.bottom + 36)
  const helperExtent = TIMELINE_GEOMETRY.laneOrigin
    + maxLane * TIMELINE_GEOMETRY.laneGap
    + TIMELINE_GEOMETRY.cardOffset + TIMELINE_GEOMETRY.cardWidth
  const mainExtent = TIMELINE_GEOMETRY.laneOrigin
    + TIMELINE_GEOMETRY.cardOffset + TIMELINE_GEOMETRY.mainCardWidth
  const width = Math.max(helperExtent, mainExtent) + TIMELINE_GEOMETRY.rightPad
  return { ...normalized, agentsById, rows, spans, spansByAgent, laneByAgent, maxLane, width, height }
}

export function formatTimelineTime(value, quality = 'exact') {
  const ms = isoMs(value)
  if (ms == null) return 'Time unavailable'
  const label = TIMELINE_CLOCK.format(new Date(ms))
  return quality === 'exact' ? label : `~${label}`
}

export function formatDuration(start, end) {
  const a = isoMs(start)
  const b = isoMs(end)
  if (a == null || b == null || b < a) return ''
  const seconds = Math.round((b - a) / 1000)
  if (seconds < 60) return '<1m'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours}h ${rest}m` : `${hours}h`
}

// ---------------------------------------------------------------------------
// Day-grouping — the journal is grouped by calendar day using each entry's ts.
// Labels: Today / Yesterday / weekday+date within the week / a plain date for
// older / "Earlier" for anything with a null or unparseable ts. Entries arrive
// newest-first; a Map keeps first-seen order so a day's entries stay together
// even if the roster ever interleaves them.
// ---------------------------------------------------------------------------

function startOfDay(ms) {
  const d = new Date(ms)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

function dayBucket(iso, now) {
  const t = iso ? Date.parse(iso) : NaN
  if (!Number.isFinite(t)) return { key: 'earlier', label: 'Earlier' }
  const today = startOfDay(now)
  const day = startOfDay(t)
  const diffDays = Math.round((today - day) / 86400000)
  const key = `d${day}`
  if (diffDays <= 0) return { key, label: 'Today' }
  if (diffDays === 1) return { key, label: 'Yesterday' }
  const d = new Date(t)
  if (diffDays < 7) {
    const weekday = d.toLocaleDateString(undefined, { weekday: 'long' })
    const md = d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
    return { key, label: `${weekday} · ${md}` }
  }
  const sameYear = d.getFullYear() === new Date(now).getFullYear()
  const label = d.toLocaleDateString(undefined,
    sameYear ? { day: 'numeric', month: 'short' }
             : { day: 'numeric', month: 'short', year: 'numeric' })
  return { key, label }
}

export function groupEntriesByDay(entries, now = Date.now()) {
  const list = Array.isArray(entries) ? entries : []
  const groups = []
  const byKey = new Map()
  for (const e of list) {
    const { key, label } = dayBucket(e && e.ts, now)
    let g = byKey.get(key)
    if (!g) { g = { key, label, items: [] }; byKey.set(key, g); groups.push(g) }
    g.items.push(e)
  }
  return groups
}

// ---------------------------------------------------------------------------
// Relative time (used for the header "Updated …" label)
// ---------------------------------------------------------------------------

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const diff = Math.max(0, now - t)
  const s = Math.floor(diff / 1000)
  if (s < 45) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d === 1) return 'yesterday'
  if (d < 7) return `${d}d ago`
  const dt = new Date(t)
  const sameYear = dt.getFullYear() === new Date(now).getFullYear()
  return dt.toLocaleDateString(undefined,
    sameYear ? { month: 'short', day: 'numeric' }
             : { month: 'short', day: 'numeric', year: 'numeric' })
}

// True when index.json is missing or older than the freshness window, so an
// on-open auto-refresh is warranted.
export function isStale(iso, now = Date.now(), thresholdMs = 2 * 60 * 1000) {
  if (!iso) return true
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return true
  return now - t > thresholdMs
}

// ---------------------------------------------------------------------------
// Markdown-lite — a safe, tiny subset for the agent's verbatim words (turn
// `original`, helper briefs/reports). Returns a plain block/span structure the
// view maps to React elements; it never produces HTML, so there is no injection
// surface.
// ---------------------------------------------------------------------------

function parseInline(text) {
  const spans = []
  const re = /(\*\*([^*]+)\*\*|`([^`]+)`)/g
  let last = 0
  let m
  while ((m = re.exec(text))) {
    if (m.index > last) spans.push({ t: 'text', v: text.slice(last, m.index) })
    if (m[2] != null) spans.push({ t: 'bold', v: m[2] })
    else if (m[3] != null) spans.push({ t: 'code', v: m[3] })
    last = m.index + m[0].length
  }
  if (last < text.length) spans.push({ t: 'text', v: text.slice(last) })
  return spans.length ? spans : [{ t: 'text', v: text }]
}

export function parseMarkdownLite(text) {
  const src = typeof text === 'string' ? text : ''
  if (!src.trim()) return []
  const lines = src.replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let para = []
  let inCode = false
  let code = []
  const flush = () => {
    if (para.length) {
      blocks.push({ type: 'para', spans: parseInline(para.join(' ')) })
      para = []
    }
  }
  for (const raw of lines) {
    const line = raw.trimEnd()
    // Fenced code block — verbatim, no inline parsing inside.
    const fence = line.match(/^\s*```/)
    if (fence) {
      if (inCode) { blocks.push({ type: 'code', text: code.join('\n') }); code = []; inCode = false }
      else { flush(); inCode = true }
      continue
    }
    if (inCode) { code.push(raw); continue }
    if (!line.trim()) { flush(); continue }
    const heading = line.match(/^(#{1,3})\s+(.*)$/)
    if (heading) { flush(); blocks.push({ type: 'heading', level: heading[1].length, spans: parseInline(heading[2]) }); continue }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/)
    if (bullet) { flush(); blocks.push({ type: 'bullet', spans: parseInline(bullet[1]) }); continue }
    const num = line.match(/^\s*(\d{1,3})[.)]\s+(.*)$/)
    if (num) { flush(); blocks.push({ type: 'num', n: num[1], spans: parseInline(num[2]) }); continue }
    para.push(line.trim())
  }
  if (inCode && code.length) blocks.push({ type: 'code', text: code.join('\n') })
  flush()
  return blocks
}
