// Workflows — a read-only window onto the background helpers your chats spin up.
// This entry owns only composition: the module tree is declared in mobius.json's
// source_files, and esbuild bundles from here.
//
//   storage.js        — read-through storage + the run-now refresh transport
//   domain.js         — pure derive/format/order helpers (the testable core)
//   theme.js          — the single app stylesheet (CSS string)
//   views/Home.jsx        — chats with background work
//   views/ChatDetail.jsx  — one chat's prompt-and-branches timeline
//
// App holds the two-level navigation (journal → layered chat timeline),
// subscribes to index.json so it repaints when the job writes, and drives
// on-demand refresh.

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import { CSS } from './theme.js'
import { makeStorage, useOnline } from './storage.js'
import { isStale, relativeTime } from './domain.js'
import { Home } from './views/Home.jsx'
import { ChatDetail } from './views/ChatDetail.jsx'

// Await a nav-open handle across runtime versions: the newer `outcome` API
// carries a status ('owned'/'standalone' both mean render the view); the older
// `ready` promise resolves true only for 'owned'. Missing nav → treat as
// success and rely on the in-app back control.
async function awaitNav(handle) {
  try {
    if (handle && handle.outcome && typeof handle.outcome.then === 'function') {
      const res = await handle.outcome
      const status = res && res.status
      return status === 'owned' || status === 'standalone'
    }
    if (handle && handle.ready && typeof handle.ready.then === 'function') {
      return await handle.ready.catch(() => false)
    }
  } catch (_) {
    return false
  }
  return true
}

export default function App({ appId, token }) {
  const storage = useMemo(() => makeStorage(appId, token), [appId, token])
  const online = useOnline()

  const [idx, setIdx] = useState(undefined)      // undefined = loading, null = absent
  const idxRef = useRef(undefined)
  const [refreshing, setRefreshing] = useState(false)

  const [chat, setChat] = useState(null)         // { chatId, meta } | null

  const chatNavRef = useRef(null)
  // Per-chat view state (scroll position + open prompt disclosures), keyed by
  // chatId. Keeping it here preserves the user's place when they return to the
  // journal and later reopen the same workflow.
  const chatViewStates = useRef(new Map())
  const autoRefreshedRef = useRef(false)
  const readyRef = useRef(false)
  const pollTimerRef = useRef(null)
  // In-flight guard (a ref, not the `refreshing` state, which updates async and
  // would let two rapid taps both pass the check) + a mounted flag so a poll
  // scheduled around an await never fires after unmount.
  const refreshInFlightRef = useRef(false)
  const mountedRef = useRef(false)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // Live index.json — repaints Home whenever the job rewrites it.
  useEffect(() => {
    const unsub = storage.subscribe('index.json', (v) => {
      idxRef.current = v
      setIdx(v)
    })
    return () => { try { unsub && unsub() } catch (_) { /* noop */ } }
  }, [storage])

  // app_ready once, after the first index load resolves.
  useEffect(() => {
    if (idx === undefined || readyRef.current) return
    readyRef.current = true
    const count = (idx && Array.isArray(idx.entries)) ? idx.entries.length : 0
    if (window.mobius && typeof window.mobius.signal === 'function') {
      window.mobius.signal('app_ready', { chat_count: count })
    }
  }, [idx])

  const clearPoll = useCallback(() => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  // On-demand refresh: fire the job, then poll index.json for a fresher
  // updated_at (bounded backoff, ~30s cap). The subscribe above does the actual
  // repaint; this only owns the "Updating…" state.
  const doRefresh = useCallback(async () => {
    if (refreshInFlightRef.current) return
    refreshInFlightRef.current = true
    const stop = () => {
      refreshInFlightRef.current = false
      if (mountedRef.current) setRefreshing(false)
    }
    const prev = (idxRef.current && idxRef.current.updated_at) || null
    setRefreshing(true)
    if (window.mobius && typeof window.mobius.signal === 'function') {
      window.mobius.signal('refresh_requested')
    }
    const ok = await storage.runJob()
    if (!ok || !mountedRef.current) { stop(); return }
    const start = Date.now()
    const tick = async () => {
      if (!mountedRef.current) { refreshInFlightRef.current = false; return }
      const cur = await storage.getJSON('index.json')
      if (!mountedRef.current) { refreshInFlightRef.current = false; return }
      if (cur && cur.updated_at && cur.updated_at !== prev) {
        idxRef.current = cur
        setIdx(cur)
        stop()
        return
      }
      if (Date.now() - start > 30000) { stop(); return }
      pollTimerRef.current = setTimeout(tick, 2500)
    }
    pollTimerRef.current = setTimeout(tick, 2500)
  }, [storage])

  // Auto-refresh once on open when the data is missing or stale.
  useEffect(() => {
    if (idx === undefined || autoRefreshedRef.current) return
    autoRefreshedRef.current = true
    if (isStale(idx && idx.updated_at)) doRefresh()
  }, [idx, doRefresh])

  useEffect(() => () => clearPoll(), [clearPoll])

  // Navigation ---------------------------------------------------------------

  const openDetail = useCallback(async (chatObj) => {
    const chatId = chatObj.chat_id
    if (window.mobius && window.mobius.nav && typeof window.mobius.nav.open === 'function') {
      const handle = window.mobius.nav.open('workflows-chat', () => {
        chatNavRef.current = null
        setChat(null)
      })
      chatNavRef.current = handle
      const okNav = await awaitNav(handle)
      if (chatNavRef.current !== handle) return
      if (!okNav) { chatNavRef.current = null; return }
    }
    setChat({ chatId, meta: chatObj })
    if (window.mobius && typeof window.mobius.signal === 'function') {
      window.mobius.signal('chat_opened')
    }
  }, [])

  const closeChat = useCallback(() => {
    const h = chatNavRef.current
    chatNavRef.current = null
    try { if (h && h.close) h.close() } catch (_) { /* noop */ }
    setChat(null)
  }, [])

  // Tear down any open nav surfaces on unmount.
  useEffect(() => () => {
    try { if (chatNavRef.current && chatNavRef.current.close) chatNavRef.current.close() } catch (_) { /* noop */ }
  }, [])

  const openShellChat = useCallback((chatId) => {
    if (!chatId) return
    try {
      if (window.parent && window.parent !== window) {
        // Target the shell's own origin rather than '*'. The app iframe is
        // same-origin with the shell, so this is its real origin; the shell
        // still re-validates the sender before acting.
        window.parent.postMessage(
          { type: 'moebius:open-chat', chatId: String(chatId) },
          window.location.origin,
        )
      }
    } catch (_) { /* noop */ }
  }, [])

  const updatedLabel = (idx && idx.updated_at)
    ? `Updated ${relativeTime(idx.updated_at)}`
    : 'Not run yet'

  let view
  if (chat) {
    view = (
      <ChatDetail
        storage={storage}
        chatId={chat.chatId}
        chatMeta={chat.meta}
        viewStates={chatViewStates.current}
        onBack={closeChat}
        onOpenChat={openShellChat}
      />
    )
  } else {
    view = (
      <Home
        appId={appId}
        idx={idx}
        loaded={idx !== undefined}
        online={online}
        refreshing={refreshing}
        updatedLabel={updatedLabel}
        onRefresh={doRefresh}
        onOpenDetail={openDetail}
        onOpenChat={openShellChat}
      />
    )
  }

  return (
    <>
      <style>{CSS}</style>
      {view}
    </>
  )
}
