#!/usr/bin/env python3
"""Digest local agent-run traces into the Workflows app's storage documents.

This is the read side of the Workflows outcome journal: it walks the
Claude CLI and Codex SDK on-disk session traces under `/data`, reconstructs
which owner chat spawned which helper (workflow phases, Task subagents, Codex
collab agents), and writes three families of storage documents the mini-app
renders — an `index.json` roster, one `chats/<chat_id>.json` per chat, and one
`helpers/<agent_id>.json` prompt document per helper. The document shapes
are the frozen schema the UI and this job both code to (see `SCHEMA_NOTES`).

Design commitments that shaped every function here:

- **Status is derived from artifacts, never model-generated.** Records sharing
  an `agent_id` are reconciled once; terminal downstream evidence supersedes an
  async launch acknowledgement. Reports and tools inform that verdict but are
  not published into the skim-first UI contract.
- **Attribution is looked up, never guessed.** A session is joined to a chat
  only through the explicit signals in `Attribution` (session-links API, then a
  Task `tool_use_id` match, then a workflow/collab parent link). We never join
  by cwd, originator, or timestamp — those rhyme by coincidence. A session that
  none of those cover lands in the `unlinked` bucket with a reason string.
- **Incremental parsing within a bounded scan budget.** Transcripts grow without
  bound; one invocation scans at most `BUDGET_BYTES` of new bytes and gives the
  parser at most `BUDGET_SECS`, persisting per-file cursors + an accumulator
  under the job state dir so the next run continues where this one stopped.
  Metadata reads and storage publication are separately bounded network work,
  so total wall time can be longer. See `read_delta` and `CursorStore`.

The job runs as the `mobius` user under app-job-runner (ordinary tier): it
reads owner chat metadata with the service token (`/data/service-token.txt`,
an owner JWT — owner routes reject the app token) and WRITES its own storage
through the HTTP storage API with the `APP_TOKEN` from the environment. It has
no dependency on any backend Python module; the one thing it borrows from the
platform — the secret-scrub regexes — is COPIED below, not imported, so the job
never couples to backend layout.
"""

from __future__ import annotations

import argparse
import heapq
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Protocol

# --- schema + budget + caps -------------------------------------------------

SCHEMA_VERSION = 4

# A single invocation scans one slice of a possibly-huge backfill. These caps
# bound trace parsing, not metadata reads or storage publication. Persist scan
# progress so the next run continues where this one stopped.
BUDGET_SECS = 10.0
BUDGET_BYTES = 25 * 1024 * 1024
API_READ_TIMEOUT_SECS = 5
API_WRITE_TIMEOUT_SECS = 5

# Maximum bytes a single JSONL record may occupy. A record larger than this is
# flagged and skipped rather than emitted (or, when it also exceeds the read
# window, stepped over) so one pathological line — e.g. a multi-MB tool output —
# can never wedge a file's cursor: without this bound a record with no newline
# inside the per-run byte budget consumes 0, the offset never advances, and
# every later record in that file is blocked forever. Kept well under
# BUDGET_BYTES so ordinary budget pressure (a small read window late in a run)
# is NOT mistaken for an oversized record.
MAX_RECORD_BYTES = 4 * 1024 * 1024

# Freshness window for the "working" verdict: a transcript touched within this
# is treated as a live helper, older is treated as terminal (stopped/finished).
FRESH_SECS = 15 * 60
# Tolerate ordinary clock skew, but never let a corrupt far-future timestamp
# keep a helper "running" for months or years.
FUTURE_SKEW_SECS = 5 * 60

# Structural caps applied at emit time so one runaway helper cannot produce an
# unbounded document.
FINAL_REPORT_CAP = 8 * 1024
FULL_PROMPT_CAP = 256 * 1024
OUTCOME_CAP = 280
DETAIL_LINE_CAP = 160

# Bound the steps retained in the persistent accumulator (before the tighter
# emit-time MAX_STEPS). Keeps job state from growing with a 5000-tool helper
# while still preserving enough head+tail to render MAX_STEPS.
ACCUM_STEP_CAP = 240

# Total self-imposed ceiling for app-side artifacts. Well under the platform's
# per-app 1 GiB storage quota; when exceeded we evict the oldest helper detail
# prompt documents (never the roster or chat summaries) — see enforce_app_cap.
APP_ARTIFACT_CAP_BYTES = 100 * 1024 * 1024
# The journal's navigable core gets a smaller budget so full-prompt leaves can
# still fit beneath APP_ARTIFACT_CAP_BYTES. These are product retention limits,
# not request-time guesses: truncation is published explicitly in schema v4.
BASE_ARTIFACT_TARGET_BYTES = 75 * 1024 * 1024
MAX_TIMELINE_AGENTS = 400
MAX_MAIN_RUNS_PER_CHAT = 100
MAX_JOURNAL_CHATS = 1000
MAX_LIFECYCLE_CACHE_EVENTS = 50_000
MAX_LIFECYCLE_CACHE_RUNS = 10_000
MAX_LIFECYCLE_CACHE_EVENTS_PER_CHAT = 2_000
# At most spawn/start/terminal for every retained helper plus 40 owner turns.
MAX_TIMELINE_EVENTS_PER_CHAT = MAX_TIMELINE_AGENTS * 3 + 40

SCHEMA_NOTES = """\
index.json  {schema, updated_at, entries:[...], history:{chats_omitted}}
chats/<id> {schema, chat_id, provider, title, outcome, prompt_full, ts, turns:[...], timeline:{...,retention}}
  turn.note: display-only completeness line ("N of M helpers never reported a
  result…") when a fleet's own journal proves more helpers launched than ever
  returned. Never an owner-action signal: a delivered turn stays completed.
helpers/<id> {schema, agent_id, chat_id, brief_full}
"""


# --- secret scrubbing -------------------------------------------------------
# COPIED from backend/app/chat_log_redaction.py (`_SECRET_PATTERNS` +
# `scrub_secrets`). Copied, not imported: the job must not couple to backend
# module layout, and this is a small, stable pattern set. If the source set
# gains a pattern, mirror it here. The patterns catch token SHAPES (not a fixed
# key list) so a new provider's format is covered when it rhymes with one of
# these; ordered most-specific-first so the generic long-token rule can't
# half-eat a JWT.
#
# ONE DELIBERATE DIVERGENCE from the source set: the generic catch-all class
# below drops `/` (source uses `[A-Za-z0-9_+/=-]{32,80}`; here `[A-Za-z0-9_+=-]`).
# Chat prose almost never contains long slashed blobs except pasted secrets, but
# this job's free text is tool STEP TITLES/DETAILS, which are dominated by file
# paths (`/data/apps/.../some_long_component.json`). With `/` in the class a
# 32-80 char path segment collapsed to `[redacted-token]`, gutting the app's
# drill-down (its core value). The four specific rules above still run first and
# catch real secrets (JWTs, sk-/ghp_/AIza… keys, bearer tokens, key=val pairs);
# only the unstructured slash-containing catch-all is relaxed. The copied-set
# invariant otherwise holds.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
  (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),
   "[redacted-jwt]"),
  (re.compile(r"\b(?:sk-ant-|sk-|rk_live_|sk_live_|ghp_|gho_|ghu_|ghs_|xox[abprs]-|AIza)[A-Za-z0-9_-]{8,}\b"),
   "[redacted-key]"),
  (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._-]{12,}"),
   r"\1 [redacted-token]"),
  (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*\S+"),
   r"\1=[redacted]"),
  (re.compile(r"\b[A-Za-z0-9_+=-]{32,80}\b"),
   "[redacted-token]"),
]


def scrub(text: Optional[str]) -> str:
  """Replaces key/token/JWT-shaped substrings with labelled markers.

  Best-effort reduced exposure, not a guarantee — a regex cannot catch a pasted
  document or an encoded value; the caps bound how much survives regardless.
  Applied to every free-text fragment that leaves this job (outcomes, briefs,
  commands, and verbatim reports).
  """
  if not text:
    return ""
  for pattern, repl in _SECRET_PATTERNS:
    text = pattern.sub(repl, text)
  return text


def clip_line(text: str, cap: int = DETAIL_LINE_CAP) -> str:
  """Scrubs then truncates a single detail/title line to `cap` chars."""
  out = scrub(text).replace("\n", " ").strip()
  return out[:cap] + "…" if len(out) > cap else out


# --- incremental cursor + accumulator store ---------------------------------

class Budget:
  """Wall-clock + bytes ceiling shared across a single refresh invocation.

  `exhausted` flips once either limit is hit; the parse loops check it before
  opening the next file so a slice always stops cleanly on a file boundary and
  persists progress rather than being killed mid-record.
  """

  def __init__(self, secs: float, max_bytes: int):
    self.deadline = time.monotonic() + secs
    self.max_bytes = max_bytes
    self.bytes_read = 0

  @property
  def remaining_bytes(self) -> int:
    return max(0, self.max_bytes - self.bytes_read)

  @property
  def exhausted(self) -> bool:
    return time.monotonic() >= self.deadline or self.remaining_bytes <= 0

  def consume(self, n: int) -> None:
    self.bytes_read += n


def load_json(path: Path, default):
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return default


def save_json(path: Path, obj) -> None:
  """Atomic write via a temp file + rename so a crash can't leave half a doc."""
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(path.suffix + ".tmp")
  tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
  os.replace(tmp, path)


class CursorStore:
  """Per-file read cursors persisted across invocations.

  A cursor is `{ino, offset, last_uuid, first_fp}`. `offset` is the byte
  position of the next unread record; `ino` and `first_fp` detect a replaced
  file. `first_fp` is a stable fingerprint of the file's first record (see
  `_first_record_fp`) — it catches a replacement that inode + size cannot:
  inode reuse, or a truncate-and-regrow that lands the new content at or above
  the old offset (so `size >= offset` and `ino` is unchanged). On the next run:
    - inode changed, current size < offset, OR the first-record fingerprint
      changed  ->  the file was replaced or truncated; rescan from 0 and let the
      caller reset that file's derived state (symmetric across the Claude and
      Codex fold paths).
    - otherwise read forward from `offset`, consuming only newline-terminated
      records so a half-written tail line is re-read intact next time.
  """

  def __init__(self, path: Path):
    self.path = path
    raw = load_json(path, {})
    self.files: dict[str, dict] = raw.get("files", {}) if isinstance(raw, dict) else {}

  def get(self, key: str) -> dict:
    return self.files.get(key, {})

  def set(self, key: str, cur: dict) -> None:
    self.files[key] = cur

  def save(self) -> None:
    save_json(self.path, {"schema": SCHEMA_VERSION, "files": self.files})


def _first_record_fp(path: Path, cap: int = 4096) -> Optional[str]:
  """Fingerprint of a file's first COMPLETE record — bytes up to the first
  newline, hashed. Used to detect a replacement the inode + size checks miss.

  Returns None until the first record is fully written (no newline in the first
  `cap` bytes yet), so a file still writing its opening line never looks
  'replaced'. Once the first record is terminated it is immutable (these files
  are append-only), so the fingerprint is stable for the file's whole life and
  changes only when the file is genuinely replaced.
  """
  try:
    with open(path, "rb") as fh:
      head = fh.read(cap)
  except OSError:
    return None
  nl = head.find(b"\n")
  if nl < 0:
    return None
  return hashlib.sha256(head[:nl]).hexdigest()[:16]


def read_delta(path: Path, cursor: dict, budget: Budget) -> tuple[bool, list[dict], dict]:
  """Reads new newline-terminated JSON records past `cursor` within `budget`.

  Returns `(rescanned, records, new_cursor)`. `rescanned` is True when the file
  was replaced/truncated and we restarted at byte 0 — the caller uses it to
  reset any state it derived from this file before folding the records back in.
  A partial tail line (no trailing newline, or budget cut the read mid-line)
  leaves `new_cursor.offset` at that line's start so it is re-read intact.

  Two robustness invariants: (1) a single record larger than `MAX_RECORD_BYTES`
  is flagged and skipped (or stepped over when it also exceeds the read window)
  so it can never wedge the cursor; (2) the byte budget is charged only for the
  bytes actually CONSUMED (advanced past), never the partial tail that gets
  re-read next run — charging the tail would double-count and shrink the budget.
  """
  try:
    st = path.stat()
  except OSError:
    return False, [], cursor
  ino, size = st.st_ino, st.st_size
  prev_off = int(cursor.get("offset", 0))
  prev_ino = cursor.get("ino")
  prev_fp = cursor.get("first_fp")
  fp = _first_record_fp(path)
  replaced = prev_fp is not None and fp is not None and prev_fp != fp
  rescanned = prev_ino is not None and (prev_ino != ino or size < prev_off or replaced)
  start = 0 if (rescanned or prev_ino is None) else prev_off

  def _cursor(off: int, uuid) -> dict:
    return {"ino": ino, "offset": off, "last_uuid": uuid, "first_fp": fp}

  if size <= start:
    return rescanned, [], _cursor(start, cursor.get("last_uuid"))
  to_read = min(size - start, budget.remaining_bytes)
  if to_read <= 0:
    return rescanned, [], _cursor(start, cursor.get("last_uuid"))
  try:
    with open(path, "rb") as fh:
      fh.seek(start)
      chunk = fh.read(to_read)
  except OSError:
    return rescanned, [], cursor
  # Keep only through the last newline: everything after it is a partial record
  # still being appended (or cut off by the byte budget).
  nl = chunk.rfind(b"\n")
  if nl < 0:
    at_eof = (start + len(chunk)) >= size
    if not at_eof and len(chunk) >= MAX_RECORD_BYTES:
      # A single record longer than both the read window AND the max allowance:
      # step the cursor past this window so it can't wedge. A later window
      # resyncs at the next newline (its leading partial line fails json.loads
      # and is dropped). Charge the bytes we advance past.
      budget.consume(len(chunk))
      return rescanned, [], _cursor(start + len(chunk), cursor.get("last_uuid"))
    # A partial tail (still being written at EOF) or ordinary budget pressure
    # mid-record: leave the offset at `start` so the record is re-read intact,
    # and charge nothing (those bytes are re-read; charging here double-counts).
    return rescanned, [], _cursor(start, cursor.get("last_uuid"))
  consumed = nl + 1
  budget.consume(consumed)
  records: list[dict] = []
  last_uuid = cursor.get("last_uuid")
  for line in chunk[:consumed].split(b"\n"):
    if not line.strip():
      continue
    if len(line) > MAX_RECORD_BYTES:
      # An oversized single record: skip rather than emit a giant step. Dropping
      # it (vs. wedging) keeps every later record in the file readable.
      continue
    try:
      rec = json.loads(line)
    except ValueError:
      continue
    if isinstance(rec, dict):
      records.append(rec)
      if rec.get("uuid"):
        last_uuid = rec["uuid"]
  return rescanned, records, _cursor(start + consumed, last_uuid)


def read_whole_if_changed(path: Path, cursor: dict) -> tuple[bool, Optional[dict], dict]:
  """Reads a whole-file JSON doc (workflow record, meta, board) if it changed.

  These files are rewritten atomically, not appended, so a mtime+size cursor is
  the right freshness signal — no byte offset. Returns `(changed, obj, cursor)`;
  `obj` is None when unchanged or unreadable.
  """
  try:
    st = path.stat()
  except OSError:
    return False, None, cursor
  sig = {"mtime": st.st_mtime, "size": st.st_size}
  if cursor.get("mtime") == sig["mtime"] and cursor.get("size") == sig["size"]:
    return False, None, cursor
  obj = load_json(path, None)
  return (obj is not None), obj, sig


# --- workflow-meta extraction ----------------------------------------------

# The workflow record embeds its meta as a JS source string
# (`export const meta = { name: '…', description: '…', phases: [{title:'…'}] }`).
# We extract with tolerant regexes and NEVER eval the script — it is arbitrary
# agent-authored JS. Missing fields degrade to empty, not an error.
_META_NAME = re.compile(r"\bname\s*:\s*(['\"])(.*?)\1", re.DOTALL)
_META_DESC = re.compile(r"\bdescription\s*:\s*(['\"])(.*?)\1", re.DOTALL)
_META_PHASES_BLOCK = re.compile(r"\bphases\s*:\s*\[(.*?)\]", re.DOTALL)
_META_PHASE_TITLE = re.compile(r"\btitle\s*:\s*(['\"])(.*?)\1", re.DOTALL)


def extract_workflow_meta(script: str) -> dict:
  """Best-effort parse of `name`, `description`, `phases[].title` from the
  workflow script string. Tolerant by design: a script that doesn't match
  yields empty fields rather than raising."""
  if not isinstance(script, str):
    return {"name": "", "description": "", "phases": []}
  name = _META_NAME.search(script)
  desc = _META_DESC.search(script)
  phases: list[dict] = []
  block = _META_PHASES_BLOCK.search(script)
  if block:
    for m in _META_PHASE_TITLE.finditer(block.group(1)):
      phases.append({"title": m.group(2), "detail": ""})
  return {
    "name": name.group(2) if name else "",
    "description": desc.group(2) if desc else "",
    "phases": phases,
  }


# --- record-shape helpers ---------------------------------------------------

def _msg_text_and_tools(msg: dict) -> tuple[Optional[str], list[tuple[str, str]]]:
  """Pulls (last_text, [(tool_name, tool_input_summary)]) from one Claude
  assistant message. Content may be a list of typed blocks or a bare string."""
  content = msg.get("content")
  if isinstance(content, str):
    return (content or None), []
  text: Optional[str] = None
  tools: list[tuple[str, str]] = []
  if isinstance(content, list):
    for block in content:
      if not isinstance(block, dict):
        continue
      if block.get("type") == "text" and block.get("text"):
        text = block["text"]
      elif block.get("type") == "tool_use":
        tools.append((str(block.get("name") or "tool"),
                      _short_input(block.get("input"))))
  return text, tools


def _msg_spawn_tool_ids(msg: dict) -> list[str]:
  """Task/Agent tool ids launched by one Claude helper message.

  Claude flattens helper transcript files inside a run. A nested child's meta
  file names the ``toolUseId`` that launched it; retaining the matching id from
  the parent's transcript gives us an exact parent edge instead of guessing
  ancestry from ``spawnDepth``.
  """
  content = msg.get("content")
  if not isinstance(content, list):
    return []
  out: list[str] = []
  for block in content:
    if (not isinstance(block, dict) or block.get("type") != "tool_use"
        or block.get("name") not in SPAWNING_TOOL_NAMES or not block.get("id")):
      continue
    tool_id = str(block["id"])
    if tool_id not in out:
      out.append(tool_id)
  return out


def _short_input(inp) -> str:
  """A compact one-line description of a tool input for a step detail."""
  if isinstance(inp, dict):
    for key in ("description", "command", "file_path", "path", "pattern", "prompt", "query"):
      if inp.get(key):
        return str(inp[key])
    return ", ".join(sorted(inp.keys()))
  return "" if inp is None else str(inp)


def _usage_tokens(msg: dict) -> int:
  u = msg.get("usage")
  if not isinstance(u, dict):
    return 0
  return int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0)


# --- accumulator model ------------------------------------------------------
# The accumulator is the compact, incrementally-grown model persisted between
# runs (state/model.json). It is NOT the app storage: storage documents are
# rebuilt from it each run. Keyed maps keep merge cheap and idempotent.
#
#   sessions[sid]  = {provider, last_activity_at, parent_thread_id, tool_use_ids:[]}
#   agents[akey]   = per-helper digest (akey = "<sid>::<agent_id>")
#   runs[rkey]     = per-run container (rkey = "<sid>::<run_id>")
#
# A run groups helpers: kind "workflow" (a wf_*.json with phases), "tasks" (Task
# subagents under a session), or "collab" (a Codex multi-agent turn).


def _new_model() -> dict:
  return {"schema": SCHEMA_VERSION, "sessions": {}, "agents": {}, "runs": {}}


def _session(model: dict, sid: str, provider: str) -> dict:
  defaults = {
    "provider": provider, "last_activity_at": None,
    "parent_thread_id": None, "spawn_depth": None,
    "spawn_label": "", "tool_use_ids": [],
  }
  s = model["sessions"].setdefault(sid, dict(defaults))
  # Schema-3 accumulators are deliberately forward-migratable. Populate every
  # newly introduced field when an existing session is first touched so an
  # incremental refresh cannot fail halfway through and strand good storage.
  for key, value in defaults.items():
    s.setdefault(key, list(value) if isinstance(value, list) else value)
  s["provider"] = provider or s["provider"]
  return s


def _bump_activity(session: dict, iso: Optional[str]) -> None:
  if iso and (session["last_activity_at"] is None or iso > session["last_activity_at"]):
    session["last_activity_at"] = iso


def _run(model: dict, sid: str, run_id: str, kind: str, label: str) -> dict:
  rkey = f"{sid}::{run_id}"
  r = model["runs"].setdefault(rkey, {
    "sid": sid, "run_id": run_id, "kind": kind, "label": label,
    "started_at": None, "ended_at": None, "phases": [], "agent_keys": [],
    "journal_started": 0, "journal_resulted": 0,
  })
  if label:
    r["label"] = label
  return r


def _agent(model: dict, sid: str, run_id: str, agent_id: str, kind: str) -> dict:
  akey = f"{sid}::{agent_id}"
  defaults = {
    "sid": sid, "run_id": run_id, "run_kind": kind, "agent_id": agent_id,
    "agent_type": "", "description": "", "tool_use_id": None, "goal": "",
    "spawn_depth": 1, "parent_agent_id": None,
    "spawned_tool_use_ids": [],
    "steps": [], "final_report": "", "tokens": 0, "started_at": None,
    "started_time_quality": "unknown", "ended_at": None,
    "ended_time_quality": "unknown", "last_ts": None,
    "has_activity": False, "result": None,
    "final_report_terminal": None, "interrupted": False,
    "board_status": None, "source_expired": False, "truncated": False,
  }
  a = model["agents"].setdefault(akey, dict(defaults))
  for key, value in defaults.items():
    a.setdefault(key, list(value) if isinstance(value, list) else value)
  a["run_id"] = run_id
  a["run_kind"] = kind
  run = _run(model, sid, run_id, kind, "")
  if akey not in run["agent_keys"]:
    run["agent_keys"].append(akey)
  return a


# --- Claude tree parsing ----------------------------------------------------

def parse_claude(cc_dir: Path, model: dict, cursors: CursorStore, budget: Budget) -> None:
  """Walks `<cc>/projects/-data` and folds every session's helper traces into
  `model`. Sessions run with cwd `/data`, so `-data` is the project dir for the
  owner's chats (the attribution scope); other project dirs are out of scope.
  """
  root = cc_dir / "projects" / "-data"
  if not root.is_dir():
    return
  tasks_root = cc_dir / "tasks"
  # Newest sessions first so a budget-limited slice covers live activity before
  # old backfill. Directories (which hold subagent traces) are the interesting
  # ones; a bare <sid>.jsonl with no dir spawned no helpers.
  for sess_dir in _sorted_by_mtime(p for p in root.iterdir() if p.is_dir()):
    if budget.exhausted:
      return
    sid = sess_dir.name
    session = _session(model, sid, "claude")
    _bump_activity(session, _mtime_iso(root / f"{sid}.jsonl"))
    _bump_activity(session, _mtime_iso(sess_dir))
    _parse_claude_workflows(sess_dir, sid, model, cursors, budget)
    _parse_claude_task_agents(sess_dir, sid, model, cursors, budget)
    _parse_task_board(tasks_root / sid, sid, model)


def _parse_claude_workflows(sess_dir: Path, sid: str, model: dict,
                            cursors: CursorStore, budget: Budget) -> None:
  wf_dir = sess_dir / "workflows"
  agents_root = sess_dir / "subagents" / "workflows"
  if not wf_dir.is_dir():
    return
  for wf_file in sorted(wf_dir.glob("wf_*.json")):
    if budget.exhausted:
      return
    changed, obj, cur = read_whole_if_changed(wf_file, cursors.get(str(wf_file)))
    cursors.set(str(wf_file), cur)
    if changed and isinstance(obj, dict):
      run_id = str(obj.get("runId") or wf_file.stem)
      meta = extract_workflow_meta(obj.get("script", ""))
      run = _run(model, sid, run_id, "workflow", meta["name"] or run_id)
      run["phases"] = meta["phases"]
      run["started_at"] = obj.get("timestamp") or run["started_at"]
      _bump_activity(_session(model, sid, "claude"), obj.get("timestamp"))
    else:
      run_id = str(wf_file.stem)
    _parse_agent_dir(agents_root / run_id, sid, run_id, "workflow", model, cursors, budget)


def _parse_claude_task_agents(sess_dir: Path, sid: str, model: dict,
                              cursors: CursorStore, budget: Budget) -> None:
  """Task-tool subagents live directly under `subagents/` (not the workflows/
  subtree). They share one synthetic per-session "tasks" run."""
  sub = sess_dir / "subagents"
  if not sub.is_dir():
    return
  has_direct = any(sub.glob("agent-*.meta.json")) or any(sub.glob("agent-*.jsonl"))
  if not has_direct:
    return
  _run(model, sid, "tasks", "tasks", "Task subagents")
  _parse_agent_dir(sub, sid, "tasks", "tasks", model, cursors, budget, journal=False)


def _parse_agent_dir(agent_dir: Path, sid: str, run_id: str, kind: str,
                     model: dict, cursors: CursorStore, budget: Budget,
                     journal: bool = True) -> None:
  """Folds every `agent-*.jsonl` (+ its `.meta.json`) in `agent_dir` into the
  model, and the sibling `journal.jsonl` results when `journal` is set."""
  if not agent_dir.is_dir():
    return
  if journal:
    _parse_journal(agent_dir / "journal.jsonl", sid, run_id, kind, model, cursors, budget)
  for tr in sorted(agent_dir.glob("agent-*.jsonl")):
    if budget.exhausted:
      return
    agent_id = tr.stem[len("agent-"):]
    agent = _agent(model, sid, run_id, agent_id, kind)
    _load_agent_meta(tr.with_name(f"agent-{agent_id}.meta.json"), agent, model, sid)
    _fold_agent_transcript(tr, agent, model, sid, cursors, budget)
  _resolve_claude_parents(model, sid, run_id)


def _resolve_claude_parents(model: dict, sid: str, run_id: str) -> None:
  """Join nested Claude children to the helper whose Task call spawned them.

  A depth number alone never identifies a parent. Missing, duplicate, or cyclic
  evidence therefore stays unknown; the public timeline renders that honestly.
  """
  agents = [a for a in model.get("agents", {}).values()
            if a.get("sid") == sid and a.get("run_id") == run_id]
  owners: dict[str, list[str]] = {}
  for candidate in agents:
    for tool_id in candidate.get("spawned_tool_use_ids", []):
      owners.setdefault(str(tool_id), []).append(str(candidate["agent_id"]))
  for child in agents:
    tool_id = child.get("tool_use_id")
    matches = owners.get(str(tool_id), []) if tool_id else []
    child_id = str(child.get("agent_id") or "")
    if len(matches) == 1 and matches[0] != child_id:
      child["parent_agent_id"] = matches[0]
    elif not child.get("_main_parent_evidence"):
      # Reconciliation runs repeatedly as incremental files grow. Do not leave
      # a formerly unique edge behind after later evidence makes it ambiguous.
      child["parent_agent_id"] = None


def _load_agent_meta(meta_path: Path, agent: dict, model: dict, sid: str) -> None:
  meta = load_json(meta_path, None)
  if not isinstance(meta, dict):
    return
  agent["agent_type"] = str(meta.get("agentType") or agent["agent_type"] or "")
  try:
    agent["spawn_depth"] = max(1, int(meta.get("spawnDepth") or agent.get("spawn_depth") or 1))
  except (TypeError, ValueError):
    agent["spawn_depth"] = max(1, int(agent.get("spawn_depth") or 1))
  if meta.get("spawnDepth") is not None and agent["spawn_depth"] == 1:
    agent["parent_agent_id"] = "main"
    agent["_main_parent_evidence"] = True
  if meta.get("description"):
    agent["description"] = str(meta["description"])
  tuid = meta.get("toolUseId")
  if tuid:
    agent["tool_use_id"] = str(tuid)
    tool_use_ids = _session(model, sid, "claude")["tool_use_ids"]
    if tuid not in tool_use_ids:
      tool_use_ids.append(str(tuid))


def _fold_agent_transcript(tr: Path, agent: dict, model: dict, sid: str,
                           cursors: CursorStore, budget: Budget) -> None:
  """Streams new transcript records into the agent digest: first user message
  is the goal, tool_use blocks become steps, the last assistant text is the
  final_report, usage accumulates into tokens. A rescanned (replaced) file
  resets the digest first so nothing double-counts."""
  rescanned, records, cur = read_delta(tr, cursors.get(str(tr)), budget)
  cursors.set(str(tr), cur)
  if rescanned:
    _reset_agent_digest(agent)
  session = _session(model, sid, "claude")
  for rec in records:
    ts = rec.get("timestamp")
    if ts:
      agent["started_at"] = agent["started_at"] or ts
      if agent.get("started_time_quality") == "unknown":
        # First transcript activity is an observed lower bound, not proof of
        # the orchestration instant at which the helper was launched.
        agent["started_time_quality"] = "observed"
      agent["last_ts"] = ts
      _bump_activity(session, ts)
    msg = rec.get("message")
    if not isinstance(msg, dict):
      continue
    role = msg.get("role")
    if role == "user":
      goal = msg.get("content")
      user_text = goal if isinstance(goal, str) else "\n".join(
        str(b.get("text") or "") for b in (goal or []) if isinstance(b, dict))
      if "[request interrupted by user]" in user_text.lower():
        agent["interrupted"] = True
        agent["has_activity"] = True
        if ts:
          agent["ended_at"] = ts
          agent["ended_time_quality"] = "exact"
      if agent["goal"]:
        continue
      if isinstance(goal, str):
        agent["goal"] = goal
      elif isinstance(goal, list):
        agent["goal"] = next((b.get("text", "") for b in goal
                              if isinstance(b, dict) and b.get("type") == "text"), "")
    if role == "assistant":
      text, tools = _msg_text_and_tools(msg)
      for tool_id in _msg_spawn_tool_ids(msg):
        if tool_id not in agent["spawned_tool_use_ids"]:
          agent["spawned_tool_use_ids"].append(tool_id)
      agent["tokens"] += _usage_tokens(msg)
      if text:
        agent["final_report"] = text
        agent["final_report_terminal"] = str(msg.get("stop_reason") or "").lower() in (
          "end_turn", "stop_sequence", "stop")
        if agent["final_report_terminal"] and ts:
          agent["ended_at"] = ts
          agent["ended_time_quality"] = "exact"
        elif not agent["final_report_terminal"]:
          agent["ended_at"] = None
          agent["ended_time_quality"] = "unknown"
        agent["interrupted"] = False
        agent["has_activity"] = True
      for name, detail in tools:
        agent["steps"].append({"kind": "tool", "title": name, "detail": detail})
        # Claude emits commentary text and its following tool-use as separate
        # records for the same model turn. Once a tool follows the text, that
        # text was progress—not a terminal handback.
        agent["final_report_terminal"] = False
        agent["interrupted"] = False
        agent["ended_at"] = None
        agent["ended_time_quality"] = "unknown"
        agent["has_activity"] = True
      _trim_accum_steps(agent)


def _trim_accum_steps(agent: dict) -> None:
  steps = agent["steps"]
  if len(steps) > ACCUM_STEP_CAP:
    half = ACCUM_STEP_CAP // 2
    agent["steps"] = steps[:half] + steps[-half:]
    agent["truncated"] = True


def _reset_agent_digest(agent: dict) -> None:
  """Drops the transcript-derived fields of a helper digest so a replaced source
  file can be re-folded from scratch without double-counting. Leaves identity
  and lookup fields (agent_type, description, tool_use_id, goal, result,
  board_status) intact — only the streamed-in accumulation is reset."""
  agent.update({"steps": [], "final_report": "", "tokens": 0,
                "spawned_tool_use_ids": [], "started_at": None,
                "started_time_quality": "unknown", "ended_at": None,
                "ended_time_quality": "unknown", "last_ts": None, "has_activity": False,
                "final_report_terminal": None, "interrupted": False})


def _parse_journal(journal_path: Path, sid: str, run_id: str, kind: str,
                   model: dict, cursors: CursorStore, budget: Budget) -> None:
  """Journal lines are `{type:started|result, agentId, result}`. A `result`
  line is the authoritative "this helper finished" signal and carries the
  helper's own reported outcome. `started` lines are counted per run (never
  folded per agent): the launched-versus-reported gap is the run-level
  evidence that a fleet ended before every helper returned — e.g. the owning
  turn was stopped mid-run — independently of which transcripts survived.

  The counters carry two integrity guards so a completeness note can never
  be a false alarm: `journal_counted_from_start` proves counting began at
  byte 0 (a cursor that pre-dates the counters leaves it unset — such runs
  simply never get a note), and `journal_caught_up` proves the last pass
  reached EOF (a budget-cut or mid-append read suppresses the note until a
  later refresh finishes the file)."""
  cursor = cursors.get(str(journal_path))
  fresh_start = cursor.get("ino") is None
  rescanned, records, cur = read_delta(journal_path, cursor, budget)
  cursors.set(str(journal_path), cur)
  if cur.get("ino") is None:
    return  # journal absent: nothing to count, nothing to reset
  run = None
  if rescanned or fresh_start:
    run = _run(model, sid, run_id, kind, "")
    run["journal_started"] = 0
    run["journal_resulted"] = 0
    run["journal_counted_from_start"] = True
  for rec in records:
    agent_id = rec.get("agentId")
    if not agent_id:
      continue
    if rec.get("type") == "started":
      run = _run(model, sid, run_id, kind, "")
      run["journal_started"] = run.get("journal_started", 0) + 1
      continue
    if rec.get("type") != "result":
      continue
    run = _run(model, sid, run_id, kind, "")
    run["journal_resulted"] = run.get("journal_resulted", 0) + 1
    _agent(model, sid, run_id, str(agent_id), kind)["result"] = rec.get("result")
  run = _run(model, sid, run_id, kind, "")
  try:
    run["journal_caught_up"] = int(cur.get("offset", 0)) >= journal_path.stat().st_size
  except OSError:
    run["journal_caught_up"] = False


def _parse_task_board(board_dir: Path, sid: str, model: dict) -> None:
  """Task-board cards (`tasks/<sid>/*.json` = `{subject,status}`) label the
  session's tasks run and surface a board-level failure. Board cards are the
  todo items, not the spawned agents, so they inform the run — not a 1:1 agent
  join (which we never fabricate).

  Board-derived status is recomputed from ALL current cards every run (cards are
  small, and re-reading them is cheap): a run that once had a failed card but
  whose card later completed or was deleted must be able to CLEAR the failure.
  We therefore clear the prior board-derived status first, then reapply only if
  a current card is still failing — a status set from a stale earlier run can
  never stick.
  """
  if not board_dir.is_dir():
    return
  rkey = f"{sid}::tasks"
  agent_keys = model["runs"].get(rkey, {}).get("agent_keys", [])
  # Clear any prior board-derived failure before recomputing from scratch.
  for akey in agent_keys:
    agent = model["agents"].get(akey)
    if agent and agent.get("board_status") == "failed":
      agent["board_status"] = None
  subjects: list[str] = []
  failed = False
  for card_path in sorted(board_dir.glob("*.json")):
    card = load_json(card_path, None)
    if not isinstance(card, dict):
      continue
    if card.get("subject"):
      subjects.append(str(card["subject"]))
    if str(card.get("status", "")).lower() in ("failed", "error", "blocked"):
      failed = True
  if subjects and rkey in model["runs"]:
    model["runs"][rkey]["label"] = subjects[0]
  if failed:
    for akey in agent_keys:
      if akey in model["agents"]:
        model["agents"][akey]["board_status"] = "failed"


# --- Codex tree parsing -----------------------------------------------------

def parse_codex(codex_home: Path, model: dict, cursors: CursorStore, budget: Budget) -> None:
  """Walks `<codex_home>/sessions/YYYY/MM/DD/rollout-*.jsonl`.

  A rollout is a session; its `session_meta` line carries the id + optional
  `parent_thread_id` linking a forked/collab child to its parent. We scan the
  body defensively for collab items (a helper spawned inside the turn). An
  ordinary Codex chat with no collab items and no children yields no runs — it
  is a top-level chat, not a helper, and only matters for attribution.
  """
  root = codex_home / "sessions"
  if not root.is_dir():
    return
  for rollout in _sorted_by_mtime(root.rglob("rollout-*.jsonl")):
    if budget.exhausted:
      return
    _parse_codex_rollout(rollout, model, cursors, budget)


def _parse_codex_rollout(rollout: Path, model: dict, cursors: CursorStore,
                         budget: Budget) -> None:
  rescanned, records, cur = read_delta(rollout, cursors.get(str(rollout)), budget)
  cursors.set(str(rollout), cur)
  # The session id is only known once we've seen session_meta; buffer collab
  # signals until then. Codex writes session_meta as the first record, so on a
  # fresh file this resolves immediately; a mid-file resume slice reuses the
  # sid already recorded on the file's cursor is not needed because the sid also
  # appears on later records via payload — but we key off session_meta which is
  # re-read on rescan.
  sid = _codex_sid_for(rollout, model, cursors)
  if rescanned:
    # A replaced/truncated rollout re-delivers records from byte 0. Reset the
    # digests this rollout derives so re-folding cannot double-append steps —
    # symmetric with _fold_agent_transcript's reset on the Claude side. The sid
    # is re-read from session_meta (record 0) below; prefer that, falling back
    # to the cached sid for a defensive rescan whose window somehow lost it.
    reset_sid = sid
    for rec in records:
      if rec.get("type") == "session_meta":
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        reset_sid = str(p.get("session_id") or p.get("id") or reset_sid or rollout.stem)
        break
    if reset_sid:
      _reset_codex_digests(model, reset_sid)
  session = _session(model, sid, "codex") if sid else None
  for rec in records:
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    rtype = rec.get("type")
    if rtype == "session_meta":
      sid = str(payload.get("session_id") or payload.get("id") or sid or rollout.stem)
      session = _session(model, sid, "codex")
      spawn = _codex_spawn_info(payload)
      # session_meta is the fresh authority, including an explicit absence of a
      # parent. Do not preserve a stale accumulated parent when the current
      # metadata says this is top-level. Invariant: a session is never its own
      # parent.
      parent = (payload.get("parent_thread_id") or payload.get("parentThreadId")
                or spawn.get("parent_thread_id"))
      session["parent_thread_id"] = None if not parent or str(parent) == sid else str(parent)
      # A forked sub-agent's assignment lives in its own session_meta source, not
      # in a user_message — carry it so the helper card is labelled by the task
      # it was spawned for rather than left blank.
      if spawn.get("label") and not session.get("spawn_label"):
        session["spawn_label"] = spawn["label"]
      if spawn.get("depth") is not None:
        session["spawn_depth"] = spawn["depth"]
      cursors.set(f"codex-sid::{rollout}", {"sid": sid})
    ts = rec.get("timestamp")
    if session and ts:
      _bump_activity(session, ts)
    if session and sid:
      _fold_codex_collab(payload, sid, model, ts)
      _fold_codex_subagent_activity(payload, sid, model, ts)
      _fold_codex_helper_activity(rtype, payload, sid, model, ts)
  if session is not None:
    _bump_activity(session, _mtime_iso(rollout))


def _codex_spawn_info(meta_payload: dict) -> dict:
  """Pulls a forked sub-agent's parent + human label out of its session_meta.

  A spawned Codex sub-agent records its origin under
  ``source.subagent.thread_spawn`` = {parent_thread_id, agent_path,
  agent_nickname, ...}. The task it was spawned for is the ``agent_path``
  (e.g. "/root/calculate_product"); ``agent_nickname`` ("Mendel") is a fallback.
  Tolerant of camel/snake and a missing source (a top-level chat returns {}).
  """
  src = meta_payload.get("source") or meta_payload.get("threadSource") or {}
  if not isinstance(src, dict):
    return {}
  sub = src.get("subagent") or src.get("subAgent") or {}
  spawn = sub.get("thread_spawn") or sub.get("threadSpawn") or {}
  if not isinstance(spawn, dict):
    return {}
  path = spawn.get("agent_path") or spawn.get("agentPath") or ""
  nick = spawn.get("agent_nickname") or spawn.get("agentNickname") or ""
  label = str(path).lstrip("/").replace("_", " ").strip() or str(nick).strip()
  return {
    "parent_thread_id": spawn.get("parent_thread_id") or spawn.get("parentThreadId"),
    "label": label,
    "depth": spawn.get("depth"),
  }


def _codex_sid_for(rollout: Path, model: dict, cursors: CursorStore) -> Optional[str]:
  """Recovers the session id for a resume slice whose current byte range does
  not re-include session_meta, from the sid cached on first parse."""
  cached = cursors.get(f"codex-sid::{rollout}")
  return cached.get("sid") if isinstance(cached, dict) else None


def _reset_codex_digests(model: dict, sid: str) -> None:
  """Resets the transcript-derived state of every collab helper this Codex
  session owns (its self-agent `sid::sid` and any collab children `sid::*`),
  so a rescanned rollout re-folds cleanly. `_fold_codex_collab` re-sets each
  child's status/goal and `_fold_codex_helper_activity` re-appends steps from
  the empty base — no duplicates."""
  for agent in model["agents"].values():
    if agent.get("sid") == sid and agent.get("run_kind") == "collab":
      _reset_agent_digest(agent)


def _enforce_parent_invariant(model: dict) -> None:
  """Repairs stale accumulators produced before the self-parent guard existed.

  Clearing only the session field is insufficient: the old fold may already
  have created a synthetic `sid::sid` helper and collab run for that fake child.
  Remove that derived self-helper while preserving any real children in the same
  run. Invariant: a session is never its own parent.
  """
  sessions = model.get("sessions", {})
  agents = model.get("agents", {})
  agent_ids = {str(agent.get("agent_id")) for agent in agents.values()
               if agent.get("agent_id")}
  # Codex records a child's parent *thread*. Convert a known top-level parent
  # thread to the public main lane, retain a known helper parent, and never
  # invent ancestry when the referenced rollout was not observed.
  for agent in agents.values():
    parent = str(agent.get("parent_agent_id") or "") or None
    if not parent or parent == "main":
      continue
    parent_session = sessions.get(parent)
    if parent_session is not None:
      if parent_session.get("parent_thread_id"):
        agent["parent_agent_id"] = parent if parent in agent_ids else None
      else:
        agent["parent_agent_id"] = "main"
    elif parent not in agent_ids:
      agent["parent_agent_id"] = None

  for sid, session in sessions.items():
    if str(session.get("parent_thread_id") or "") != sid:
      continue
    session["parent_thread_id"] = None
    akey = f"{sid}::{sid}"
    agent = model.get("agents", {}).get(akey)
    if not agent or agent.get("run_kind") != "collab":
      continue
    model["agents"].pop(akey, None)
    for rkey, run in list(model.get("runs", {}).items()):
      if akey in run.get("agent_keys", []):
        run["agent_keys"] = [key for key in run["agent_keys"] if key != akey]
        if not run["agent_keys"]:
          model["runs"].pop(rkey, None)


def _looks_collab(payload: dict) -> bool:
  """True when a rollout payload is a Codex collab tool call — matched on the
  discriminator OR the shape (defensive: the typed field naming may be camel or
  snake across SDK versions, and no live sample exists yet)."""
  t = str(payload.get("type", ""))
  if "collabAgentToolCall" in t or "collab_agent_tool_call" in t:
    return True
  keys = set(payload.keys())
  camel = {"senderThreadId", "receiverThreadIds", "agentsStates"}
  snake = {"sender_thread_id", "receiver_thread_ids", "agents_states"}
  return bool(keys & camel) or bool(keys & snake)


def _fold_codex_collab(payload: dict, sid: str, model: dict,
                       ts: Optional[str] = None) -> None:
  """A collab item names spawned agents in `agents_states` (thread_id -> state
  with status/model). Each becomes a helper under this session's collab run.

  CHILD LIFECYCLE IS PER-CHILD, NOT PER-CALL. A spawned child's done-ness is
  derived ONLY from ITS OWN terminal `CollabAgentState.status` (keyed here by
  the child's thread_id — the same set as `receiver_thread_ids`), NEVER from the
  collab tool-call itself completing. This matters because one collab op call
  (spawn/wait/send/close) completing says nothing about whether the child it
  targets has terminated: a wait/send/close op returns while the child is still
  inProgress. Reading the per-child status snapshot is what keeps a still-running
  child reported as `working` until its own state flips to completed/failed.

  Constraint for the RUNNER side (out of this file's scope): the emitter must
  likewise key child lifecycle by `receiver_thread_ids` + terminal child status,
  not by turning every completed collab op into a `task_done`. This parser is
  already per-child-correct; the runner's `_tool_completed_events` /
  `_tool_start_event` are where that fix lands before collab is enabled.
  """
  if not _looks_collab(payload):
    return
  states = payload.get("agents_states") or payload.get("agentsStates") or {}
  if not isinstance(states, dict):
    return
  _run(model, sid, "collab", "collab", "Codex collab")
  for thread_id, state in states.items():
    if not isinstance(state, dict):
      continue
    agent = _agent(model, sid, "collab", str(thread_id), "collab")
    agent["parent_agent_id"] = sid
    agent["agent_type"] = str(state.get("model") or agent["agent_type"] or "codex")
    status = str(state.get("status", ""))
    # inProgress/completed/failed are the typed CollabAgentState values; map to
    # the frozen derived-status vocabulary. Left as a hint on the digest;
    # derive_status turns it into the final verdict. A later collab record with
    # a fresher status overwrites this, so a terminal status supersedes an
    # earlier inProgress; a status-less op leaves the last known status intact.
    agent["result"] = {"collab_status": status} if status else agent["result"]
    for field in ("prompt", "goal"):
      if state.get(field) and not agent["goal"]:
        agent["goal"] = str(state[field])
    agent["has_activity"] = True
    if ts and not agent.get("started_at"):
      agent["started_at"] = ts
      agent["started_time_quality"] = "observed"


def _fold_codex_subagent_activity(payload: dict, sid: str, model: dict,
                                  record_ts: Optional[str]) -> None:
  """Fold Codex's persisted per-child lifecycle marker.

  Unlike the current live SDK's generic collab wait, the rollout marker carries
  stable child identity plus a native event timestamp. It is therefore the
  strongest historical start/interruption evidence available to the fallback.
  """
  ptype = str(payload.get("type") or "")
  if ptype not in ("sub_agent_activity", "subAgentActivity"):
    return
  child_id = payload.get("agent_thread_id") or payload.get("agentThreadId")
  if not child_id:
    return
  child_id = str(child_id)
  agent = _agent(model, sid, "collab", child_id, "collab")
  agent["agent_type"] = agent.get("agent_type") or "codex"
  agent["parent_agent_id"] = sid
  path = payload.get("agent_path") or payload.get("agentPath")
  if path and not agent.get("goal"):
    agent["goal"] = str(path).lstrip("/").replace("_", " ")
  native_occurred = _coerce_iso(payload.get("occurred_at_ms")
                                or payload.get("occurredAtMs"))
  occurred = native_occurred or record_ts
  quality = "exact" if native_occurred else "observed" if record_ts else "unknown"
  kind = str(payload.get("kind") or "").lower()
  if kind == "started":
    if occurred and not agent.get("started_at"):
      agent["started_at"] = occurred
      agent["started_time_quality"] = quality
    agent["has_activity"] = True
  elif kind in ("interrupted", "stopped", "cancelled", "canceled"):
    agent["interrupted"] = True
    agent["has_activity"] = True
    if occurred:
      agent["ended_at"] = occurred
      agent["ended_time_quality"] = quality


def _fold_codex_helper_activity(rtype: Optional[str], payload: dict, sid: str,
                                model: dict, ts: Optional[str] = None) -> None:
  """When THIS session is a collab/forked CHILD (has a parent), its own turn is
  a helper's transcript: record its function calls as steps and its last agent
  message as the report, on a synthetic self-agent under the parent's link.

  This only produces a helper when the session has a parent_thread_id; a
  top-level Codex chat takes neither branch. The child is stitched to the
  parent's chat later, in attribution.
  """
  session = model["sessions"].get(sid) or {}
  if (not session.get("parent_thread_id")
      or str(session.get("parent_thread_id")) == sid):
    return
  agent = _agent(model, sid, "collab", sid, "collab")
  agent["parent_agent_id"] = str(session["parent_thread_id"])
  try:
    agent["spawn_depth"] = max(1, int(session.get("spawn_depth") or 1))
  except (TypeError, ValueError):
    agent["spawn_depth"] = 1
  if ts and not agent.get("started_at"):
    agent["started_at"] = ts
    agent["started_time_quality"] = "exact"
  if ts:
    agent["last_ts"] = ts
  if not agent["agent_type"]:
    agent["agent_type"] = "codex"
  if session.get("spawn_label") and not agent["goal"]:
    agent["goal"] = str(session["spawn_label"])
  if rtype == "response_item" and payload.get("type") == "function_call":
    agent["steps"].append({"kind": "tool", "title": str(payload.get("name") or "tool"),
                           "detail": _short_input(_json_or_none(payload.get("arguments")))})
    agent["final_report_terminal"] = False
    agent["interrupted"] = False
    agent["has_activity"] = True
    _trim_accum_steps(agent)
  elif rtype == "event_msg" and payload.get("type") == "agent_message":
    if payload.get("message"):
      agent["final_report"] = str(payload["message"])
      agent["final_report_terminal"] = payload.get("phase") == "final_answer"
      agent["interrupted"] = False
      agent["has_activity"] = True
  elif rtype == "event_msg" and payload.get("type") == "task_complete":
    if payload.get("last_agent_message"):
      agent["final_report"] = str(payload["last_agent_message"])
    agent["final_report_terminal"] = True
    agent["interrupted"] = False
    agent["has_activity"] = True
    agent["result"] = {"collab_status": "completed"}
    completed_at = _coerce_iso(payload.get("completed_at")
                               or payload.get("completedAt")) or ts
    started_at = _coerce_iso(payload.get("started_at") or payload.get("startedAt"))
    duration_ms = payload.get("duration_ms") or payload.get("durationMs")
    if not started_at and completed_at and isinstance(duration_ms, (int, float)):
      completed_epoch = _iso_to_epoch(completed_at)
      if completed_epoch is not None and 0 <= float(duration_ms) < 365 * 24 * 3600 * 1000:
        started_at = _epoch_to_iso(completed_epoch - float(duration_ms) / 1000)
    if started_at:
      agent["started_at"] = started_at
      agent["started_time_quality"] = "exact"
    if completed_at:
      agent["ended_at"] = completed_at
      agent["ended_time_quality"] = "exact"
  elif rtype == "event_msg" and payload.get("type") == "turn_aborted":
    agent["final_report_terminal"] = False
    agent["interrupted"] = True
    agent["has_activity"] = True
    if ts:
      agent["ended_at"] = ts
      agent["ended_time_quality"] = "exact"
  elif rtype == "event_msg" and payload.get("type") == "user_message" and not agent["goal"]:
    agent["goal"] = str(payload.get("text") or payload.get("message") or "")


def _json_or_none(s):
  if isinstance(s, (dict, list)):
    return s
  try:
    return json.loads(s)
  except (ValueError, TypeError):
    return s


# --- attribution ------------------------------------------------------------

class Attribution:
  """Joins a session id to an owner chat id through explicit signals only.

  Priority (strict; first hit wins, never a guess):
    1. the session-links API (session_id -> chat_id, backend-authoritative);
    2. a Task `tool_use_id` seen on one of the session's subagents that also
       appears as a Task tool block in some chat's messages;
    3. a workflow/tasks run inherits its parent session's chat (same sid);
    4. a Codex child inherits its `parent_thread_id`'s chat (recursively);
    5. otherwise unlinked, with a reason string.

  Steps 1-2 are data handed in at construction; 3 is implicit (helpers key off
  their own sid); 4 is resolved in `resolve` by walking parent links.

  `links` is keyed by the backend's true link identity `(provider, session_id)`,
  not by session_id alone: a Claude session id can equal a Codex thread id, and
  keying by id alone lets whichever row the API returned last silently shadow
  the other. A legacy provider-less link shape is stored under `(None, sid)` and
  consulted only when no provider-qualified link matches.
  """

  def __init__(self, links: dict[tuple, str], tooluse_to_chat: dict[str, str],
               chats: dict[str, dict]):
    self.links = links
    self.tooluse_to_chat = tooluse_to_chat
    self.chats = chats

  def resolve(self, sid: str, sessions: dict[str, dict],
              _seen: Optional[set] = None) -> tuple[Optional[str], str]:
    session = sessions.get(sid, {})
    provider = session.get("provider")
    # 1. session-links API, looked up on the session's OWN provider so a
    #    same-id session of a different provider can never claim this link.
    if (provider, sid) in self.links:
      return self.links[(provider, sid)], "session-link"
    if (None, sid) in self.links:  # legacy provider-less link shape
      return self.links[(None, sid)], "session-link"
    # 2. Task tool_use_id match.
    for tuid in session.get("tool_use_ids", []):
      if tuid in self.tooluse_to_chat:
        return self.tooluse_to_chat[tuid], "task-tool-use"
    # 4. Codex child inherits its parent_thread_id's chat (recursively).
    parent = session.get("parent_thread_id")
    if parent == sid:
      # Defensive read-side enforcement for an accumulator created by an older
      # parser: a session is never its own parent.
      parent = None
    if parent:
      seen = _seen or set()
      if parent in seen:
        return None, "parent-cycle"
      seen.add(sid)
      chat_id, reason = self.resolve(parent, sessions, seen)
      if chat_id:
        return chat_id, "parent-thread-link"
      # Propagate the real recursive reason: a cycle deeper in the chain must
      # surface as parent-cycle, not be masked as a plain parent-unlinked.
      return None, "parent-cycle" if reason == "parent-cycle" else "parent-unlinked"
    return None, "no-link"


def fetch_session_links(base_url: str, token: str) -> tuple[bool, dict[tuple, str]]:
  """GET /api/chats/session-links with the owner service token.

  Returns `(ok, links)` where `links` maps `(provider, session_id) -> chat_id`.

  `ok` is False ONLY on a genuine fetch FAILURE (missing token, network error,
  timeout, 5xx, or a malformed 2xx body) — the caller aborts the publish so a
  transient outage can't wipe the last-good documents. A 404 (the endpoint is
  absent on older instances) or an empty 2xx body is `ok=True` with an empty
  map: attribution simply falls through to the tool_use_id / parent strategies,
  which is NOT a failure.

  Keyed by `(provider, session_id)` — the backend's true link identity — so a
  Claude session id equal to a Codex thread id cannot collide. Rows carry a
  provider; a provider-less legacy shape is keyed `(None, session_id)` and
  resolve() falls back to it only when no provider-qualified link matched.
  Tolerant of several response shapes so a minor contract drift doesn't silently
  drop every link.
  """
  status, data = _api_get_json(base_url, "/api/chats/session-links", token)
  if status == 404:
    return True, {}          # endpoint absent -> empty, fall through (not a failure)
  if status != 200:
    return False, {}         # missing token / network / 5xx / malformed -> failure
  out: dict[tuple, str] = {}
  if isinstance(data, dict) and isinstance(data.get("links"), list):
    data = data["links"]
  if isinstance(data, dict):
    for k, v in data.items():
      if isinstance(v, str):
        out[(None, str(k))] = v
      elif isinstance(v, dict) and v.get("chat_id"):
        out[(v.get("provider"), str(k))] = str(v["chat_id"])
  elif isinstance(data, list):
    for row in data:
      if isinstance(row, dict) and row.get("session_id") and row.get("chat_id"):
        out[(row.get("provider"), str(row["session_id"]))] = str(row["chat_id"])
  return True, out


def fetch_chats(base_url: str, token: str) -> tuple[bool, dict[str, dict]]:
  """GET /api/chats — the roster of chats with title, activity and the
  durable open-question marker. The list endpoint omits provider, so provider
  is filled from the session trace.

  Returns `(ok, chats)`. `ok` is False on any fetch FAILURE (missing token,
  network error, timeout, non-2xx, or a non-list body) — the caller must NOT
  rebuild storage from a failed roster (every session would fall to `unlinked`
  and overwrite/delete the good documents). A 2xx list body is a SUCCESS even
  when empty (a genuinely empty roster is `ok=True, {}`).
  """
  status, data = _api_get_json(base_url, "/api/chats?include_app_chats=true", token)
  if status != 200 or not isinstance(data, list):
    return False, {}
  out: dict[str, dict] = {}
  for c in data:
    if isinstance(c, dict) and c.get("id"):
      out[str(c["id"])] = {
        "title": c.get("title") or "Untitled chat",
        "provider": c.get("provider"),
        "created_by_app_id": c.get("created_by_app_id"),
        "activity_at": c.get("activity_at") or c.get("updated_at"),
        "waiting_for_input": bool(c.get("pending_question_id")),
        "input_kind": "question" if c.get("pending_question_id") else None,
      }
  return True, out


LIFECYCLE_PAGE_LIMIT = 1000
LIFECYCLE_MAX_PAGES = 20
LIFECYCLE_CACHE_SCHEMA = 3
_LIFECYCLE_TYPES = {"agent_spawned", "agent_started", "agent_terminal"}
_LIFECYCLE_STATES = {"running", "done", "failed", "stopped"}
_TIME_QUALITIES = {"exact", "observed", "estimated", "unknown"}


def _bounded_lifecycle_id(value, *, storage_key: bool = False) -> Optional[str]:
  """Bound opaque API identities; storage-key ids may not alter URL paths."""
  if value is None:
    return None
  result = str(value)
  if not result or len(result.encode("utf-8", "replace")) > 256:
    return None
  if storage_key and (result in (".", "..") or "/" in result or "\\" in result
                      or any(ord(char) < 32 for char in result)):
    return None
  return result


def _lifecycle_iso(value) -> Optional[str]:
  result = _coerce_iso(value)
  return result if _iso_to_epoch(result) is not None else None


def _normalized_lifecycle_event(raw: dict) -> Optional[dict]:
  """Validate and bound one owner lifecycle API event.

  Unknown event types are ignored for forward compatibility. Identity and
  chronology fields remain structural; arbitrary provider payloads and prompts
  are never copied into app state.
  """
  if not isinstance(raw, dict):
    return None
  chat_id = _bounded_lifecycle_id(raw.get("chat_id"), storage_key=True)
  logical_agent_id = _bounded_lifecycle_id(raw.get("agent_id"), storage_key=True)
  agent_id = _bounded_lifecycle_id(
    raw.get("agent_run_id") or raw.get("agent_id"), storage_key=True)
  if not chat_id or not agent_id or not logical_agent_id:
    return None
  raw_type = str(raw.get("type") or "").replace(".", "_")
  aliases = {
    "agent_completed": "agent_terminal", "agent_failed": "agent_terminal",
    "agent_stopped": "agent_terminal", "agent_interrupted": "agent_terminal",
  }
  event_type = aliases.get(raw_type, raw_type)
  if event_type not in _LIFECYCLE_TYPES:
    return None
  state = str(raw.get("state") or "").lower()
  if event_type == "agent_terminal" and state not in _LIFECYCLE_STATES - {"running"}:
    return None
  if event_type != "agent_terminal":
    state = "running"
  try:
    seq = int(raw.get("id"))
  except (TypeError, ValueError):
    return None
  if seq < 0:
    return None
  event_key = str(raw.get("event_key") or raw.get("source_event_id") or f"row:{seq}")
  digest = hashlib.sha256(event_key.encode("utf-8", "replace")).hexdigest()[:24]
  quality = str(raw.get("time_quality") or "unknown").lower()
  if quality not in _TIME_QUALITIES:
    quality = "unknown"
  occurred = _lifecycle_iso(raw.get("occurred_at"))
  observed = _lifecycle_iso(raw.get("observed_at"))
  if quality == "exact" and not occurred:
    quality = "observed" if observed else "unknown"
  elif quality == "observed" and not (occurred or observed):
    quality = "unknown"
  parent_kind = str(raw.get("parent_kind") or (
    "agent" if raw.get("parent_agent_id") else "unknown"))
  return {
    "id": seq, "event_id": f"platform-{digest}",
    "chat_id": chat_id,
    "chat_run_id": _bounded_lifecycle_id(raw.get("chat_run_id")),
    "provider": clip_line(str(raw.get("provider") or ""), 32),
    "provider_session_id": _bounded_lifecycle_id(raw.get("provider_session_id")),
    "agent_id": agent_id,
    "logical_agent_id": logical_agent_id,
    "provider_agent_id": _bounded_lifecycle_id(raw.get("provider_agent_id")),
    "parent_agent_id": (
      "main" if parent_kind == "main" else
      _bounded_lifecycle_id(
        raw.get("parent_agent_run_id") or raw.get("parent_agent_id"))
      if parent_kind == "agent" else None
    ),
    "parent_kind": parent_kind,
    "parent_source_id": _bounded_lifecycle_id(raw.get("parent_source_id")),
    "type": event_type, "state": state,
    "agent_type": clip_line(str(raw.get("agent_type") or ""), 48),
    "summary": clip_line(str(raw.get("summary") or ""), OUTCOME_CAP),
    "occurred_at": occurred, "observed_at": observed,
    "time_quality": quality,
    "source": clip_line(str(raw.get("source") or "platform"), 32),
    "source_event_id": _bounded_lifecycle_id(raw.get("source_event_id")),
  }


def _normalized_lifecycle_run(raw: dict) -> Optional[dict]:
  if not isinstance(raw, dict):
    return None
  run_id = _bounded_lifecycle_id(raw.get("id"))
  chat_id = _bounded_lifecycle_id(raw.get("chat_id"), storage_key=True)
  if not run_id or not chat_id:
    return None
  status = str(raw.get("status") or "").lower()
  try:
    update_id = max(0, int(raw.get("update_id") or 0))
  except (TypeError, ValueError):
    return None
  return {
    "id": run_id, "chat_id": chat_id,
    "update_id": update_id,
    "provider": clip_line(str(raw.get("provider") or ""), 32),
    "status": status,
    "started_at": _lifecycle_iso(raw.get("started_at")),
    "ended_at": _lifecycle_iso(raw.get("ended_at")),
  }


def fetch_agent_lifecycle(base_url: str, token: str, after_id: int = 0,
                          runs_after_id: int = 0,
                          max_pages: int = LIFECYCLE_MAX_PAGES,
                          chat_id: Optional[str] = None,
                          ) -> tuple[bool, bool, list[dict], list[dict], int, int]:
  """Fetch new platform lifecycle pages.

  Returns ``(ok, supported, events, runs, event_cursor, run_cursor)``. A 404 means an
  older platform and is a healthy, unsupported result; every other failed or
  malformed page leaves the caller's last-good cache untouched.
  """
  cursor = max(0, int(after_id or 0))
  run_cursor = max(0, int(runs_after_id or 0))
  events: list[dict] = []
  runs_by_id: dict[str, dict] = {}
  for page_index in range(max_pages):
    path = (f"/api/chats/agent-lifecycle?after_id={cursor}"
            f"&runs_after_id={run_cursor}"
            f"&limit={LIFECYCLE_PAGE_LIMIT}&run_limit={LIFECYCLE_PAGE_LIMIT}")
    if chat_id is not None:
      path += "&chat_id=" + urllib.parse.quote(chat_id, safe="")
    status, data = _api_get_json(base_url, path, token)
    if status == 404:
      return True, False, [], [], cursor, run_cursor
    if status != 200 or not isinstance(data, dict):
      return False, True, [], [], after_id, runs_after_id
    page = data.get("events")
    runs = data.get("runs")
    if not isinstance(page, list) or not isinstance(runs, list):
      return False, True, [], [], after_id, runs_after_id
    page_events: list[Optional[dict]] = []
    for row in page:
      if not isinstance(row, dict):
        return False, True, [], [], after_id, runs_after_id
      raw_type = str(row.get("type") or "").replace(".", "_")
      if raw_type not in (_LIFECYCLE_TYPES | {
          "agent_completed", "agent_failed", "agent_stopped", "agent_interrupted"}):
        continue  # additive future event type
      page_events.append(_normalized_lifecycle_event(row))
    if any(row is None for row in page_events):
      return False, True, [], [], after_id, runs_after_id
    events.extend(row for row in page_events if row is not None)
    for raw_run in runs:
      run = _normalized_lifecycle_run(raw_run)
      if run is None:
        return False, True, [], [], after_id, runs_after_id
      runs_by_id[run["id"]] = run
    try:
      next_cursor = int(data.get("next_after_id", cursor))
      next_run_cursor = int(data.get("next_runs_after_id", run_cursor))
    except (TypeError, ValueError):
      return False, True, [], [], after_id, runs_after_id
    if (next_cursor < cursor or next_run_cursor < run_cursor
        or (data.get("has_more") and next_cursor == cursor)
        or (data.get("runs_has_more") and next_run_cursor == run_cursor)):
      return False, True, [], [], after_id, runs_after_id
    cursor = next_cursor
    run_cursor = next_run_cursor
    if not data.get("has_more") and not data.get("runs_has_more"):
      return True, True, events, list(runs_by_id.values()), cursor, run_cursor
  # A bounded invocation may stop mid-backfill. The returned cursor/events are a
  # complete prefix and safe to commit; the next refresh continues from there.
  return True, True, events, list(runs_by_id.values()), cursor, run_cursor


def merge_lifecycle_state(state, events: list[dict], runs: list[dict],
                          cursor: int, *, runs_cursor: Optional[int] = None,
                          preferred_chat_ids: Iterable[str] = (),
                          pinned_chat_ids: Iterable[str] = (),
                          count_new_events: bool = True) -> dict:
  """Idempotently fold one API prefix into the persisted last-good cache.

  Scoped recovery can legitimately replay IDs older than the global tail. The
  cache therefore prefers the current owner roster instead of blindly keeping
  only the numerically newest IDs, and records total ingested facts separately
  so any cache-level omission remains visible in the published contract.
  """
  base = state if isinstance(state, dict) else {}
  by_event = {str(row.get("event_id")): row for row in base.get("events", [])
              if isinstance(row, dict) and row.get("event_id")}
  for event in events:
    by_event[event["event_id"]] = event
  by_run = {str(row.get("id")): row for row in base.get("runs", [])
            if isinstance(row, dict) and row.get("id")}
  for run in runs:
    if run.get("status") == "deleted":
      by_run.pop(run["id"], None)
    else:
      by_run[run["id"]] = run
  try:
    base_cursor = max(0, int(base.get("after_id") or 0))
  except (TypeError, ValueError):
    base_cursor = 0
  try:
    new_cursor = max(0, int(cursor or 0))
  except (TypeError, ValueError):
    new_cursor = base_cursor
  try:
    base_runs_cursor = max(0, int(base.get("runs_after_id") or 0))
  except (TypeError, ValueError):
    base_runs_cursor = 0
  try:
    new_runs_cursor = max(0, int(runs_cursor or 0)) if runs_cursor is not None else base_runs_cursor
  except (TypeError, ValueError):
    new_runs_cursor = base_runs_cursor
  seen_by_chat: dict[str, int] = {}
  raw_seen = base.get("events_seen_by_chat")
  if isinstance(raw_seen, dict):
    for chat_id, count in raw_seen.items():
      if not isinstance(chat_id, str) or not chat_id:
        continue
      try:
        seen_by_chat[chat_id] = max(0, int(count or 0))
      except (TypeError, ValueError):
        continue
  if count_new_events:
    for event in events:
      # Global cursor pages never replay an ID. Restrict the accounting to the
      # newly consumed suffix so a scoped snapshot cannot double-count facts.
      if int(event.get("id") or 0) <= base_cursor:
        continue
      chat_id = str(event.get("chat_id") or "")
      if chat_id:
        seen_by_chat[chat_id] = seen_by_chat.get(chat_id, 0) + 1

  preferred = {str(chat_id) for chat_id in preferred_chat_ids if chat_id}
  pinned = {str(chat_id) for chat_id in pinned_chat_ids if chat_id}
  per_chat: dict[str, list[dict]] = {}
  for event in by_event.values():
    per_chat.setdefault(str(event.get("chat_id") or ""), []).append(event)
  bounded_events: list[dict] = []
  for chat_events in per_chat.values():
    chat_events.sort(key=lambda row: (row.get("id", 0), row["event_id"]))
    bounded_events.extend(chat_events[-MAX_LIFECYCLE_CACHE_EVENTS_PER_CHAT:])
  retained_events = sorted(bounded_events, key=lambda row: (
    2 if str(row.get("chat_id") or "") in pinned else
    1 if str(row.get("chat_id") or "") in preferred else 0,
    row.get("id", 0), row["event_id"],
  ))[-MAX_LIFECYCLE_CACHE_EVENTS:]
  retained_events.sort(key=lambda row: (row.get("id", 0), row["event_id"]))
  retained_runs = sorted(by_run.values(), key=lambda row: (
    2 if str(row.get("chat_id") or "") in pinned else
    1 if str(row.get("chat_id") or "") in preferred else 0,
    _iso_to_epoch(row.get("started_at")) or float("-inf"), row["id"],
  ))[-MAX_LIFECYCLE_CACHE_RUNS:]
  retained_runs.sort(key=lambda row: (
    _iso_to_epoch(row.get("started_at")) or float("-inf"), row["id"]))
  known_chat_ids = {
    str(chat_id) for chat_id in base.get("known_lifecycle_chat_ids", [])
    if isinstance(chat_id, str) and chat_id
  }
  known_chat_ids.update(str(row.get("chat_id")) for row in events
                        if row.get("chat_id"))
  return {
    # Bump this schema whenever the parser learns a lifecycle event type. A
    # mismatch resets the cursor in run_refresh, making additive platform rows
    # replayable without retaining arbitrary unknown provider payloads here.
    "schema": LIFECYCLE_CACHE_SCHEMA, "after_id": max(base_cursor, new_cursor),
    "runs_after_id": max(base_runs_cursor, new_runs_cursor),
    "events": retained_events,
    "runs": retained_runs,
    "events_seen_by_chat": seen_by_chat,
    "known_lifecycle_chat_ids": sorted(known_chat_ids),
    "visible_chat_ids": sorted({
      str(chat_id) for chat_id in base.get("visible_chat_ids", [])
      if isinstance(chat_id, str) and chat_id
    }),
  }


# The tool block a chat records when it spawns a background helper. The name
# is NOT stable across agent-SDK versions — the same delegation surfaces as
# "Task" on some pins and "Agent" on others — and a chat only has to disagree
# with this set once for every helper in it to fall into the unlinked bucket
# with no error anywhere. Matching the set (rather than one literal) keeps a
# transcript readable across a version bump in either direction.
SPAWNING_TOOL_NAMES = ("Task", "Agent")


def build_tooluse_map(base_url: str, token: str, chats_meta: dict[str, dict],
                      scanned: dict[str, str], budget: Budget,
                      max_fetches: int = 12) -> dict[str, str]:
  """Fallback attribution index: `Task tool_use_id -> chat_id`, built by
  scanning a BOUNDED number of chats per run for Task tool blocks. Progressive:
  `scanned` grows across runs so the whole roster is eventually covered without
  ever fetching all chats in one slice.

  `scanned` is a `chat_id -> activity_at-when-scanned` cursor, NOT a plain set,
  so two staleness bugs are avoided:
    - A chat is recorded scanned ONLY after a SUCCESSFUL fetch; a failed GET
      leaves it unscanned so a later run retries it (a transient 500 no longer
      permanently strands a chat's Task ids in the unlinked bucket).
    - A chat is RE-scanned when its `activity_at` advances past the value we
      stored: a Task helper spawned later inside an already-scanned chat is
      picked up on the next run instead of staying unlinked forever.
  Most-recently-active chats are scanned first so live activity is covered
  within the per-run fetch budget before old backfill.
  """
  out: dict[str, str] = {}
  fetched = 0
  ordered = sorted(chats_meta.items(),
                   key=lambda kv: kv[1].get("activity_at") or "", reverse=True)
  for chat_id, meta in ordered:
    if fetched >= max_fetches or budget.exhausted:
      break
    activity = meta.get("activity_at") or ""
    prev = scanned.get(chat_id)
    if prev is not None and prev >= activity:
      continue  # already scanned at this activity mark; nothing new to find
    status, payload = _api_get_json(base_url, f"/api/chats/{chat_id}?limit=400", token)
    fetched += 1
    if status != 200 or not isinstance(payload, dict):
      continue  # do NOT mark scanned on failure -> retried on a later run
    scanned[chat_id] = activity
    for msg in payload.get("messages", []):
      for block in (msg.get("blocks") or []) if isinstance(msg, dict) else []:
        if not isinstance(block, dict) or not block.get("tool_use_id"):
          continue
        if block.get("tool") in SPAWNING_TOOL_NAMES:
          out[str(block["tool_use_id"])] = chat_id
  return out


# ---------------------------------------------------------------------------
# Helpers derived from the chat's OWN Agent block
# ---------------------------------------------------------------------------
# A chat records every helper it spawns as an `Agent` tool block: the input
# carries the goal and the helper type, the output carries whatever the helper
# handed back. That is enough to answer "what ran, what did it do, how did it
# end" without any new instrumentation — and unlike the local-trace route it is
# attributed PERFECTLY, because the block is already inside a known chat. On
# this instance the trace route resolved 3 chats while the blocks cover 21.

_ERROR_HEAD = re.compile(
  r"^\s*(API Error\b|error\b|failed\b|Traceback \(most recent call last\))", re.I)


def _agent_field(text: str, name: str) -> Optional[str]:
  """One spawn argument out of an Agent block's input.

  The input reaches us as a STRING in one of two shapes — a Python-dict repr
  (`{'description': '...'}`) or comma-separated `key=value` — and either can be
  clipped mid-value because long tool inputs are truncated. Rather than commit
  to one grammar, pull each field independently and tolerate a value that runs
  off the end.
  """
  if not isinstance(text, str) or not text:
    return None
  for pattern in (
    rf"['\"]{name}['\"]\s*:\s*'((?:[^'\\]|\\.)*)'",   # quoted, closed
    rf"['\"]{name}['\"]\s*:\s*'((?:[^'\\]|\\.)*)$",   # quoted, truncated
    rf"(?:^|,\s*){name}=([\s\S]*?)(?=,\s*[A-Za-z_][A-Za-z0-9_]*=|$)",
  ):
    hit = re.search(pattern, text)
    if hit:
      value = (hit.group(1) or "")
      value = (value.replace("\\n", "\n").replace("\\t", "\t")
               .replace("\\'", "'").replace('\\"', '"').strip())
      if value:
        return value
  return None


def _split_agent_output(output) -> tuple[Optional[str], Optional[str], Optional[str]]:
  """`(body, agent_id, usage)` — the helper's result separated from bookkeeping.

  A returned payload concatenates three different things: the actual result, a
  line naming the agent so it can be resumed, and a usage block. Split them so
  the result can be shown alone and the bookkeeping does not leak into a summary
  the reader is trying to skim.
  """
  raw = output if isinstance(output, str) else ""
  agent_hit = re.search(r"agentId:\s*([A-Za-z0-9_-]+)", raw)
  usage_hit = re.search(r"<usage>([\s\S]*?)</usage>", raw)
  body = re.sub(r"<usage>[\s\S]*?</usage>", "", raw)
  body = re.sub(r"^.*agentId:\s*[A-Za-z0-9_-]+.*$", "", body, flags=re.M).strip()
  return (body or None,
          agent_hit.group(1) if agent_hit else None,
          usage_hit.group(1) if usage_hit else None)


_FINISHED_WORDS = {"done", "finished", "completed", "complete", "success"}
_WORKING_WORDS = {"running", "in_progress", "in-progress", "working", "started"}
_STOPPED_WORDS = {"stopped", "cancelled", "canceled", "interrupted", "aborted"}

# A spawn returns one of these messages when it has only queued background work.
# They are launch receipts, never completion reports. Match the whole payload so
# a real report that merely mentions an acknowledgement is not discarded.
_ASYNC_ACK = re.compile(
  r"^\s*(?:Async agent launched successfully\.?|"
  r"Codex Task started in (?:the )?background(?:\s+as\s+\S+)?\.?(?:\s+Check\s+"
  r"/codex:status\s+\S+\s+for progress\.?)?)\s*$", re.I)
_ASYNC_ACK_ENVELOPE = re.compile(
  r"^\s*Async agent launched successfully\.\s*"
  r"\(This tool result is internal metadata\b[\s\S]*?\)\s*"
  r"The agent is working in the background\.", re.I)


def _is_async_ack(text) -> bool:
  if not isinstance(text, str):
    return False
  return bool(_ASYNC_ACK.fullmatch(text) or _ASYNC_ACK_ENVELOPE.match(text))


_PROGRESS_REPORT = re.compile(
  r"(?:\b(?:now\s+)?let me\s+(?:read|scan|fetch|search|inspect|check|look|start|"
  r"continue|run|verify|examine|trace|review|test|open|find|analy[sz]e|compile)\b|"
  r"\bi(?:'|’)ll\s+(?:read|scan|fetch|search|inspect|check|look|start|continue|"
  r"run|verify|examine|trace|review|test|open|find|analy[sz]e|compile)\b|"
  r"\b(?:compiling|continuing|investigating|checking|reading|searching|verifying|"
  r"reviewing|testing)\s+(?:the|a|an|this|that|my|our)\b)", re.I)


def _looks_progress_report(text) -> bool:
  """Conservative migration fallback for cached digests created before the
  parser retained explicit terminal markers. It catches procedural handoffs,
  but deliberately does not match phrases such as "let me know" that commonly
  appear in genuine final answers."""
  return isinstance(text, str) and bool(_PROGRESS_REPORT.search(text))


def helper_from_agent_block(block: dict, ordinal: int = 0,
                            scope: str = "") -> Optional[dict]:
  """One bounded helper-evidence record derived from an `Agent` tool block.

  The recorded status is NOT trustworthy on its own: in production, helpers
  whose payload is an outright API error are still written down as `done`. When
  the payload contradicts the status the payload wins, because a helper that
  returned an error did not do the work whatever the bookkeeping says. The
  payload wins, so downstream assembly never repeats a comfortable lie.
  """
  if not isinstance(block, dict):
    return None
  raw_input = block.get("input")
  raw_input = raw_input if isinstance(raw_input, str) else ""
  body, agent_id, _usage = _split_agent_output(block.get("output"))
  # Some chat renderers persist placeholder Task blocks with either an empty
  # input or only "Working in the background", plus an empty output. Neither
  # shape records an assignment, helper identity, or lifecycle evidence; the
  # tool-use call id is not an agent id. Publishing it creates invented helper
  # rows named "No brief was recorded".
  if (not raw_input.strip()
      or raw_input.strip().lower() == "working in the background"
      ) and not str(body or "").strip():
    return None
  is_async = _is_async_ack(body)

  if body is None:
    kind = "none"
  elif _ERROR_HEAD.match(body[:200]):
    kind = "error"
  else:
    kind = "result"

  status_word = str(block.get("status") or "").strip().lower()
  if kind == "error":
    state = "failed"
  elif is_async:
    # The launch acknowledgement is not a completion report. A later result
    # block or downstream transcript may supersede this working state when all
    # evidence for the agent_id is reconciled during document assembly.
    state = "working"
  elif status_word in _FINISHED_WORDS:
    state = "finished"
  elif status_word in _WORKING_WORDS:
    state = "working"
  elif status_word in _STOPPED_WORDS:
    state = "stopped"
  else:
    state = "unavailable"

  description = _agent_field(raw_input, "description")
  prompt = _agent_field(raw_input, "prompt")
  goal = description or ((prompt or "").split("\n")[0].strip() or None)
  return {
    "agent_id": (agent_id or block.get("tool_use_id")
                 or _stable_agent_id(goal, raw_input, ordinal, scope)),
    # description + agent_type come from arbitrary tool INPUT, so they get the
    # same scrub + cap every other free-text field that leaves this job gets —
    # a spawn prompt can quote a secret just as a report can. None stays None
    # ("not recorded"), never a placeholder.
    "description": clip_line(goal) if goal else None,
    "agent_type": clip_line(_agent_field(raw_input, "subagent_type"), 48) or None,
    "status": state,
    # A helper's returned payload is arbitrary text and can quote a secret.
    "_full_outcome": _cap_report(scrub(body)) if body and not is_async else None,
    "_brief_full": clip_markdown(prompt, FULL_PROMPT_CAP) or "",
    # A launch hint only; joined terminal evidence supersedes it later.
    "is_async": is_async,
    "_tool_use_id": str(block.get("tool_use_id") or "") or None,
    "_spawned_at": None,
  }


def _handback(blocks: list, start: int, max_actions: int = 5) -> dict:
  """What the chat did with a helper's result, read from the blocks that follow
  the `Agent` block at `start`.

  This is the "how did it get merged back in" half of a helper's story, and the
  transcript already answers it: the parent's next text block is usually it
  saying what the result means, and the tool calls after that are it acting on
  the result. Scanning STOPS at the next spawn, so one helper is never credited
  with the follow-up work of another.

  Deliberately named for what it can prove — this is what the chat did NEXT,
  which is evidence of a handback, not proof of causation. The UI must not
  promise more than that, so nothing here is called "merged".
  """
  note: Optional[str] = None
  actions: list[dict] = []
  truncated = False
  for block in blocks[start + 1:]:
    if not isinstance(block, dict):
      continue
    if block.get("type") == "text":
      if note is None:
        text = clip_line(scrub(str(block.get("content") or "")).strip(), 240)
        note = text or None
      continue
    if block.get("type") != "tool":
      continue
    if block.get("tool") in SPAWNING_TOOL_NAMES:
      break
    if len(actions) >= max_actions:
      truncated = True
      break
    actions.append({
      "tool": str(block.get("tool") or "tool"),
      "target": clip_line(scrub(str(block.get("input") or "")).strip(), 90) or None,
    })
  return {"note": note, "actions": actions, "actions_truncated": truncated}


def _stable_agent_id(goal: Optional[str], raw_input: str, ordinal: int,
                     scope: str = "") -> str:
  """A deterministic id for a helper whose payload never named one, so the same
  block keeps the same identity across runs instead of churning storage.

  The block's position in the chat is part of the seed because two valid spawns
  can still carry identical prompts. They must not collapse into one record;
  position is the remaining stable discriminator in an append-only transcript.
  """
  # `scope` is the chat id when available. Helper prompt documents are globally
  # addressed as helpers/<agent_id>.json, so identical blank/trimmed blocks in
  # two chats must not collide.
  seed = f"{scope}|{ordinal}|{goal or ''}|{raw_input[:200]}"
  return "blk" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:15]


# Ceiling so a chat document cannot grow without bound. The newest tool-using
# turns are retained; each helper's full prompt remains in its own document.
MAX_CHAT_TURNS = 40


def clip_markdown(text: Optional[str], cap: int) -> Optional[str]:
  """Scrub + byte-cap free text while keeping its markdown line structure."""
  if not text:
    return None
  out = scrub(str(text)).strip()
  if not out:
    return None
  encoded = out.encode("utf-8")
  if len(encoded) <= cap:
    return out
  # Reserve three bytes for U+2026 and discard a partial trailing code point.
  return encoded[:max(0, cap - 3)].decode("utf-8", "ignore") + "…"


def _owner_request(text: str) -> str:
  """Remove injected context envelopes before using the first user request.

  Restored legacy chats can prepend `<agent_context>...</agent_context>` to the
  owner's actual message. Treating that system-owned prelude as a title source
  produced titles such as "<agent_context> # Agent experience".
  """
  out = scrub(text).strip()
  envelope = re.compile(
    r"^\s*<(?:agent_context|environment_context|system-reminder)>[\s\S]*?"
    r"</(?:agent_context|environment_context|system-reminder)>\s*", re.I)
  while envelope.match(out):
    out = envelope.sub("", out, count=1).strip()
  return clip_markdown(out, FULL_PROMPT_CAP) or ""


def _walk_chat(messages: list, scope: str = "") -> tuple[list[dict], list[dict]]:
  """Collect helper evidence and every tool-using assistant turn in one pass.

  Schema v3 deliberately includes a tool turn even when it did not spawn a helper: the
  owner's requested Beat Machine validator example is such a turn, and omitting
  it would make the outcome journal skip the exact handback that records failure.
  Private `_facts`/`_agent_ids` fields are bounded, scrubbed parse evidence; the
  public document is derived from them later after downstream helpers are joined.
  """
  helpers: list[dict] = []
  turns: list[dict] = []
  first_request = ""
  last_user_ts = None
  for msg in messages:
    if not isinstance(msg, dict):
      continue
    if msg.get("role") == "user":
      content = msg.get("content")
      if isinstance(content, str) and content.strip() and not first_request:
        first_request = _owner_request(content)
      if isinstance(msg.get("ts"), (int, float, str)):
        last_user_ts = msg.get("ts")
      continue
    blocks = msg.get("blocks") or []
    if not any(isinstance(b, dict) and b.get("type") == "tool" for b in blocks):
      continue
    note_texts: list[str] = []
    tools: list[dict] = []
    agent_ids: list[str] = []
    for index, block in enumerate(blocks):
      if not isinstance(block, dict):
        continue
      btype = block.get("type")
      if btype == "text":
        text = clip_markdown(block.get("content"), FINAL_REPORT_CAP)
        if text:
          note_texts.append(text)
      elif btype == "tool":
        if block.get("tool") in SPAWNING_TOOL_NAMES:
          record = helper_from_agent_block(
            block, ordinal=len(helpers), scope=scope)
          if not record:
            continue
          handback = _handback(blocks, index)
          record["_handback"] = handback
          record["_spawned_at"] = msg.get("ts") if isinstance(
            msg.get("ts"), (int, float, str)) else last_user_ts
          helpers.append(record)
          agent_ids.append(str(record["agent_id"]))
        else:
          tools.append({
            "tool": clip_line(str(block.get("tool") or "tool"), 80),
            "status": clip_line(str(block.get("status") or ""), 40),
            "input": clip_markdown(str(block.get("input") or ""), 1600) or "",
            "output": clip_markdown(str(block.get("output") or ""), 2400) or "",
          })
    original = note_texts[-1] if note_texts else ""
    turns.append({
      "ts": msg.get("ts") if isinstance(msg.get("ts"), (int, float, str)) else last_user_ts,
      "_agent_ids": agent_ids,
      "_facts": _compact_turn_facts(tools, original),
      "_original": original,
      "_first_request": "",
    })
  # Messages arrive oldest-first; retain the newest bounded window.
  turns = turns[-MAX_CHAT_TURNS:]
  if turns and first_request:
    turns[0]["_first_request"] = first_request
  return helpers, turns


def _pending_secure_input(messages: list) -> bool:
  """Whether the durable transcript contains a secure-input card awaiting the owner.

  Secure values never enter the transcript; this reads only the request id and
  safe receipt status already rendered by the chat UI. Later statuses for the
  same request win so filled, settled, cancelled and expired cards cannot keep
  a workflow marked as waiting.
  """
  states: dict[str, str] = {}
  for msg in messages:
    if not isinstance(msg, dict):
      continue
    for block in msg.get("blocks") or []:
      if not isinstance(block, dict) or block.get("type") != "secure_input":
        continue
      request_id = str(block.get("request_id") or "")
      if request_id:
        states[request_id] = str(block.get("status") or "pending").lower()
  return any(status == "pending" for status in states.values())


def scan_chat_helpers(base_url: str, token: str, chats_meta: dict[str, dict],
                      scanned: dict[str, str], budget: Budget,
                      max_fetches: Optional[int] = None
                      ) -> tuple[dict[str, list[dict]], dict[str, list[dict]],
                                 dict[str, str], set[str]]:
  """Helper/turn evidence plus concrete pending secure-input receipts.

  Returns helper records, timeline turns, pending-input kinds and the set of
  rescanned chat ids. Scanning is bounded and progressive on the SAME cursor
  contract as build_tooluse_map: most-recently-active first, a chat is only
  marked scanned after a successful fetch, and it is re-scanned when its activity
  advances. Helpers and turns come from one walk (`_walk_chat`) so a branch and
  its helper share an id.

  The per-run cap exists so an unattended refresh stays inside its time budget,
  but it also sets how long a first backfill takes to reach old history: the
  cap is per run, and the newest chats are not the ones that ran helpers. The
  cursor makes catching up safe to do in bigger strides, so the limit is
  overridable via `WORKFLOWS_MAX_CHAT_SCANS` for a one-time sweep.
  """
  if max_fetches is None:
    try:
      max_fetches = int(os.environ.get("WORKFLOWS_MAX_CHAT_SCANS", "") or 40)
    except ValueError:
      max_fetches = 40
  out: dict[str, list[dict]] = {}
  turns_out: dict[str, list[dict]] = {}
  inputs_out: dict[str, str] = {}
  rescanned: set[str] = set()
  fetched = 0
  ordered = sorted(chats_meta.items(),
                   key=lambda kv: kv[1].get("activity_at") or "", reverse=True)
  for chat_id, meta in ordered:
    if fetched >= max_fetches or budget.exhausted:
      break
    activity = meta.get("activity_at") or ""
    prev = scanned.get(chat_id)
    if prev is not None and prev >= activity:
      continue
    status, payload = _api_get_json(base_url, f"/api/chats/{chat_id}?limit=400", token)
    fetched += 1
    if status != 200 or not isinstance(payload, dict):
      continue
    scanned[chat_id] = activity
    rescanned.add(chat_id)
    messages = payload.get("messages", [])
    helpers, turns = _walk_chat(messages, scope=chat_id)
    if helpers:
      out[chat_id] = helpers
    if turns:
      turns_out[chat_id] = turns
    if _pending_secure_input(messages):
      inputs_out[chat_id] = "secure_input"
  # `rescanned` is every chat freshly walked this slice, empty results included,
  # so the caller can DROP stale state for a chat whose spawns were later
  # compacted away — an overlay-only merge could never clear it otherwise.
  return out, turns_out, inputs_out, rescanned


def _api_get_json(base_url: str, path: str, token: str) -> tuple[Optional[int], object]:
  """GETs `path` with the bearer token. Returns `(status, data)`:

    - `(200, <json>)` on a 2xx with a parseable body — the only trustworthy
      SUCCESS (the body may still be an empty list/dict: an empty SUCCESS).
    - `(<code>, None)` for an HTTP error response (4xx/5xx), so a caller can
      treat a 404 as "endpoint absent" but a 500 as a failure.
    - `(None, None)` for a network-level failure (timeout, refused, DNS), a
      malformed 2xx body, or a MISSING TOKEN.

  Distinguishing a failure from an empty success is load-bearing: a caller that
  rebuilds storage from an all-empty roster would DELETE the last-good documents
  (see run_refresh). A `None` status or a non-2xx status both mean "do not trust
  this as data".
  """
  if not token:
    return None, None
  try:
    req = urllib.request.Request(
      base_url.rstrip("/") + path,
      headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=API_READ_TIMEOUT_SECS) as resp:
      status = getattr(resp, "status", None) or 200
      try:
        return status, json.load(resp)
      except ValueError:
        return None, None
  except urllib.error.HTTPError as exc:
    return exc.code, None
  except (urllib.error.URLError, OSError):
    return None, None


# --- derived status ---------------------------------------------------------

def derive_status(agent: dict, now: float) -> str:
  """Maps a helper digest to the frozen status vocabulary from ARTIFACTS ONLY.

  Order matters: explicit terminal failure/result wins over every launch or
  freshness hint. An interruption wins over prose, and only a report carrying
  an explicit terminal marker is accepted as completion on newly parsed data.
  A narrow procedural-text fallback exists solely for cached v2 accumulators
  that predate those markers. A fresh transcript remains working.
  """
  if agent.get("board_status") == "failed" or _result_is_failure(agent.get("result")):
    return "failed"
  if _result_is_success(agent.get("result")):
    return "finished"
  if _result_is_stopped(agent.get("result")) or agent.get("interrupted"):
    return "stopped"
  report = agent.get("final_report")
  result = agent.get("result")
  ack = (report if _is_async_ack(report)
         else result if _is_async_ack(result)
         else None)
  if ack:
    return "working" if _is_fresh(agent.get("last_ts"), now) else "stopped"
  if _is_fresh(agent.get("last_ts"), now):
    return "working"
  if agent.get("final_report_terminal") is True:
    return "finished"
  if agent.get("final_report_terminal") is False:
    return "stopped" if agent.get("has_activity") else "unavailable"
  # Cached v2 accumulators predate `final_report_terminal`. Preserve genuine
  # historical handbacks, but demote clearly procedural text instead of
  # repeating the old "any last assistant text means success" mistake.
  if agent.get("final_report"):
    return "stopped" if _looks_progress_report(agent["final_report"]) else "finished"
  if agent.get("source_expired"):
    return "unavailable"
  if agent.get("has_activity"):
    return "stopped"
  return "unavailable"


def _result_is_failure(result) -> bool:
  if isinstance(result, str):
    return result.strip().lower() in ("failed", "failure", "error")
  if not isinstance(result, dict):
    return False
  status = str(result.get("collab_status", "")).lower()
  if status in ("failed", "error"):
    return True
  verdict = str(result.get("verdict", "")).lower()
  return verdict in ("failed", "error")


def _result_is_stopped(result) -> bool:
  stopped = ("stopped", "cancelled", "canceled", "interrupted", "aborted")
  if isinstance(result, str):
    return result.strip().lower() in stopped
  if not isinstance(result, dict):
    return False
  return str(result.get("collab_status") or result.get("status") or "").lower() in stopped


def _result_is_success(result) -> bool:
  """A journal result (any non-failure dict) is the authoritative finished
  signal. A collab state that is still inProgress is NOT success."""
  if result is None:
    return False
  if _is_async_ack(result):
    return False
  if isinstance(result, str):
    status = result.strip().lower()
    if status in (_WORKING_WORDS | _STOPPED_WORDS | {"failure", "error"}):
      return False
    return bool(status)
  if isinstance(result, dict):
    status = str(result.get("collab_status", "")).lower()
    if status in (_WORKING_WORDS | _STOPPED_WORDS):
      return False
    return True
  return True


def _is_fresh(ts_iso: Optional[str], now: float) -> bool:
  epoch = _iso_to_epoch(ts_iso)
  if epoch is None:
    return False
  age = now - epoch
  return -FUTURE_SKEW_SECS <= age < FRESH_SECS


# --- document assembly ------------------------------------------------------

_APP_NOUNS = {
  "10": "Beat Machine", "12": "News", "39": "Reflection",
  "89": "Workflows", "beat-machine": "Beat Machine",
  "beatmachine": "Beat Machine", "news": "News", "notes": "Notes",
  "workflows": "Workflows", "reflection": "Reflection",
  "habit-tracker": "Habit Tracker", "recipes": "Recipes",
  "cuberun": "CubeRun", "cube-run": "CubeRun",
}

_DELIVERY_START = re.compile(
  r"^(?:done\b|fixed\b|added\b|built\b|here(?:'|’)s\b|updated\b|replaced\b|created\b|removed\b)", re.I)
_PROCEDURAL_START = re.compile(
  r"^(?:let me\b|let(?:'|’)s\b|checking\b|server\b|now\b|next\b|then\b|first\b|curl\b|bash\b|python\b|grep\b|npm\b|pnpm\b|\$)", re.I)
_NEGATIVE_EVIDENCE = re.compile(
  r"(?:\"valid\"\s*:\s*false|\b(?:failed|failure|error|invalid)\b|won't mount|does not load|not found)", re.I)
_POSITIVE_EVIDENCE = re.compile(
  r"(?:\"valid\"\s*:\s*true|\btests? pass(?:ed)?\b|\bverified\b|\bvalidation passed\b|\bbuild succeeded\b)", re.I)
_VERIFY_COMMAND = re.compile(
  r"(?:pytest|vitest|jest|npm\s+(?:run\s+)?(?:test|build|lint)|pnpm\s+(?:test|build|lint)|/validate\b|validator\b|\btsc\b|playwright)", re.I)
_HEDGE_EVIDENCE = re.compile(
  r"(?:\bnot\s+100%\s+sure\b|\bhopefully\b|\bi\s+(?:think|believe)\b|"
  r"\bin\s+(?:theory|principle)\b|\bassuming\b|\bpresumably\b|"
  r"\b(?:should(?:\s+be)?|ought\s+to|likely(?:\s+to)?|probably(?:\s+will)?|"
  r"might|may)\s+(?:now\s+|still\s+|not\s+)*(?:be\s+)?"
  r"(?:work(?:ing)?|load(?:ing)?|open(?:ing)?|render(?:ing)?|run(?:ning)?|"
  r"show(?:ing)?|display(?:ing)?|behave|succeed|pass|survive|hold|"
  r"fine|okay|ok|ready|done|complete|correct|good|stable|safe)\b|"
  r"\b(?:i|we)\s+(?:(?:will|can)\s+|(?:'|’)ll\s+)?(?:try|attempt)\b)", re.I)
_STOPPED_EVIDENCE = re.compile(
  r"(?:\bcould(?:n(?:'|’)t| not)\s+complete\b|\bunable\s+to\s+complete\b|"
  r"\b(?:the\s+)?work\s+stopped\b|\bi\s+stopped\s+(?:the\s+)?work\b)", re.I)


def _canonical_state(value: Optional[str]) -> str:
  state = str(value or "").lower()
  if state in ("done", "finished", "returned", "complete", "completed"):
    return "done"
  if state in ("running", "working", "launched", "in_progress", "inprogress"):
    return "running"
  if state in ("failed", "error"):
    return "failed"
  return "stopped"


def _resolved_state(states: Iterable[str]) -> str:
  """One lifecycle answer for every surface.

  Evidence is joined by agent_id before this is called. Terminal evidence is
  more resolved than a launch acknowledgement: failure wins, then a returned
  result, then a terminal stop, and only then an unresolved running/launch
  record. This precedence is shared by roster, turn cards, and helper pages.
  """
  values = {_canonical_state(s) for s in states}
  for state in ("failed", "done", "stopped", "running"):
    if state in values:
      return state
  return "stopped"


_TASK_VERBS = re.compile(
  r"^(?:please\s+)?(?:audit|analy[sz]e|build|check|compare|design|diagnose|find|fix|"
  r"implement|inspect|investigate|map|probe|research|review|test|trace|verify)\b", re.I)
_PROMPT_PREAMBLE = re.compile(
  r"^(?:you are\b|never\b|allowed\b|do not\b|don(?:'|’)t\b|important\b|"
  r"context\b|platform shape\b|your final output\b|read-only\b)", re.I)
_CONTEXT_LINE = re.compile(
  r"^(?:there is\b|the (?:owner|partner|platform|app|system)\b|in some chats\b|"
  r"symptoms?\b|background\b|known context\b|constraints?\b|rules?\b)", re.I)
_EXPLICIT_TASK = re.compile(
  r"^(?:priority\s+track|task|assignment|goal|scope|focus|subsystem|claim|finding|"
  r"your\s+(?:area|task|assignment|focus)|workstream|deliverable)\s*"
  r"(?:—|–|-|:)\s*(.+)$", re.I)


def _json_object_after(raw: str, marker: str) -> Optional[dict]:
  """Decode the first JSON object following a semantic marker.

  Spawn prompts commonly wrap the unique assignment in `Place: {...}` or
  `reported this finding: {...}` after a long reusable policy envelope. Parsing
  that object is both more specific and safer than treating a lone `{` as the
  task summary. Invalid or example JSON simply falls through to line scoring.
  """
  match = re.search(marker, raw, re.I)
  if not match:
    return None
  start = raw.find("{", match.end())
  if start < 0 or start - match.end() > 160:
    return None
  try:
    value, _ = json.JSONDecoder().raw_decode(raw[start:])
  except (TypeError, ValueError, json.JSONDecodeError):
    return None
  return value if isinstance(value, dict) else None


def _structured_task(raw: str) -> str:
  """Prefer the unique subject embedded in known structured prompt shapes."""
  app = re.search(r"(?im)^\s*app\s*:\s*([^\n(]+)", raw)
  if app:
    name = app.group(1).strip().rstrip(".")
    if name:
      return f"Audit {name}"

  place = _json_object_after(raw, r"\bplace\s*:")
  if place:
    name = str(place.get("name") or "").strip()
    if name:
      return f"Fact-check {name}"

  finding = _json_object_after(
    raw, r"(?:reported\s+(?:this\s+)?finding|claimed\s+(?:issue|finding)|finding)\s*:")
  if finding:
    title = str(finding.get("title") or finding.get("name") or "").strip()
    if title:
      return f"Verify {title}"

  hypothesis = _json_object_after(raw, r"\bhypothesis\s*:")
  if hypothesis:
    title = str(hypothesis.get("title") or hypothesis.get("claim") or "").strip()
    if title:
      return f"Verify {title}"

  proposed_fix_object = _json_object_after(raw, r"\bproposed\s+fix\s*:")
  if proposed_fix_object:
    subject = str(
      proposed_fix_object.get("change_description")
      or proposed_fix_object.get("title")
      or proposed_fix_object.get("id")
      or "").strip()
    if subject:
      return f"Review {subject}"

  proposed_fix = re.search(
    r"(?im)^[ \t]*proposed\s+fix[ \t]*:[ \t]*(?:\n[ \t]*)?([^\n]+)",
    raw)
  if proposed_fix:
    subject = proposed_fix.group(1).strip()
    if subject and subject not in ("{", "["):
      return f"Review {subject}"
  return ""


def _clean_task_line(line: str) -> str:
  line = re.sub(
    r"^(?:please\s+|your task is to\s+|i need you to\s+|i need to\s+)",
    "", line, flags=re.I).strip()
  line = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)[0].strip()
  line = line[:1].upper() + line[1:] if line else line
  return clip_line(line, 140)


def _task_summary_ranked(text: Optional[str]) -> tuple[str, int]:
  """Return `(summary, specificity)` for one prompt or short description."""
  raw = scrub(str(text or "")).strip()
  if not raw:
    return "", 0

  structured = _structured_task(raw)
  if structured:
    return _clean_task_line(structured), 4

  lines = []
  for raw_line in raw.splitlines():
    line = re.sub(r"^[#>*\-\d.)\s]+", "", raw_line).strip()
    if line and re.search(r"[A-Za-z0-9]", line):
      lines.append(line)

  # Scan the whole prompt for an explicit assignment marker before accepting a
  # generic uppercase heading. Long delegation prompts often begin with shared
  # SYMPTOM / CONTEXT sections and put the differentiating "YOUR AREA" near the
  # end; returning the first heading made several genuinely different helpers
  # look as if they had received the same task.
  for line in lines:
    explicit = _EXPLICIT_TASK.match(line)
    if explicit and not re.match(r"^(?:context|constraints?|rules?)\b", line, re.I):
      return _clean_task_line(explicit.group(1)), 3

  for line in lines:
    named = re.match(r"^[A-Z][A-Z\s_-]{3,}\s*(?:—|:|-)\s*(.+)$", line)
    if named and not re.match(
        r"^(?:context|constraints?|rules?|symptoms?|evidence|confirmed cases?|"
        r"background|files?|requirements?|known facts?)\b", line, re.I):
      return _clean_task_line(named.group(1)), 3

  imperative = next((line for line in lines if _TASK_VERBS.match(line)), "")
  if imperative:
    return _clean_task_line(imperative), 2

  fallback = next((line for line in lines
                   if not _PROMPT_PREAMBLE.match(line)
                   and not _CONTEXT_LINE.match(line)), "")
  if fallback:
    return _clean_task_line(fallback), 1
  return (_clean_task_line(lines[0]), 1) if lines else ("", 0)


def _task_summary(text: Optional[str]) -> str:
  """Extract one skimmable assignment line from a possibly policy-heavy prompt.

  Helper prompts often begin with a long persona/safety envelope. The timeline
  needs the task, not that envelope, so prefer a named track or an imperative
  line and fall back to the first substantive sentence."""
  return _task_summary_ranked(text)[0]


def _plain_ask(text: Optional[str], prompt: Optional[str] = None) -> str:
  short, short_rank = _task_summary_ranked(text)
  full, full_rank = _task_summary_ranked(prompt)
  # A structured or explicitly-labelled subject in the full prompt should beat
  # a reusable generic description. On ties, preserve the intentionally short
  # description written by the spawning agent.
  if full_rank > short_rank:
    return full
  return short or full or "No brief was recorded"


def _kind_and_name(agent_type: Optional[str], provider: str = "") -> tuple[str, str]:
  raw = f"{agent_type or ''} {provider}".lower()
  if any(word in raw for word in ("explore", "research", "search")):
    return "explore", "Explorer"
  if "codex" in raw:
    return "codex", "Codex"
  if any(word in raw for word in ("build", "implement", "frontend", "code")):
    return "build", "Helper"
  return "general", "Helper"


def _tool_verb(tool: str) -> tuple[str, str]:
  name = tool.lower().replace("-", "_")
  if name in ("read", "read_file"):
    return "read", "files"
  if name in ("edit", "multiedit", "apply_patch", "applypatch"):
    return "edited", "files"
  if name in ("write", "write_file", "create_file"):
    return "wrote", "files"
  if name in ("bash", "shell", "exec_command", "run_command"):
    return "ran", "commands"
  if name in ("grep", "glob", "rg", "search", "find"):
    return "searched", "code"
  return "used", "tools"


def _did_from_tools(tools: list[dict]) -> list[dict]:
  counts: dict[tuple[str, str], int] = {}
  for tool in tools:
    key = _tool_verb(str(tool.get("tool") or "tool"))
    counts[key] = counts.get(key, 0) + 1
  ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0][0]))[:4]
  return [{"verb": verb, "label": label, "count": count}
          for (verb, label), count in ordered]


def _acts_from_did(did: list[dict]) -> list[str]:
  return [f"{step['verb']} ×{step['count']}" for step in did]


def _commands_from_tools(tools: list[dict], cap: int = 12) -> list[str]:
  commands: list[str] = []
  for tool in tools:
    detail = clip_line(str(tool.get("input") or ""), 240)
    if detail and detail not in commands:
      commands.append(detail)
    if len(commands) >= cap:
      break
  return commands


def _trace_evidence(agent: dict, now: float, provider: str) -> dict:
  status = _canonical_state(derive_status(agent, now))
  tools = [{"tool": str(step.get("title") or "tool"),
            "input": str(step.get("detail") or ""), "output": "",
            "status": ""} for step in agent.get("steps", [])]
  result = agent.get("result")
  structured = ""
  if isinstance(result, dict):
    structured = next((str(result[k]) for k in ("summary", "message", "verdict")
                       if result.get(k)), "")
  elif isinstance(result, str):
    structured = result
  raw_report = agent.get("final_report") or structured
  report = "" if _is_async_ack(raw_report) else _cap_report(raw_report)
  brief = clip_markdown(agent.get("goal"), FULL_PROMPT_CAP) or ""
  return {
    "agent_id": str(agent["agent_id"]), "agent_type": agent.get("agent_type") or "",
    "ask": _plain_ask(agent.get("description"), agent.get("goal")),
    "brief_full": brief, "state": status, "report_full": report,
    "depth": max(1, int(agent.get("spawn_depth") or 1)),
    "tools": tools, "next": "", "provider": provider, "origin": "trace",
    "ts": agent.get("started_at") or agent.get("last_ts"),
    "parent_agent_id": agent.get("parent_agent_id"),
    "started_at": agent.get("started_at"),
    "started_time_quality": agent.get("started_time_quality") or "unknown",
    "ended_at": agent.get("ended_at"),
    "ended_time_quality": agent.get("ended_time_quality") or "unknown",
    "last_activity_at": agent.get("last_ts"),
    # Timeline state deliberately ignores freshness. A trace with activity but
    # no terminal marker is unresolved, not proof that a process is still live.
    "lifecycle_state": _trace_lifecycle_state(agent),
  }


def _trace_lifecycle_state(agent: dict) -> str:
  if agent.get("board_status") == "failed" or _result_is_failure(agent.get("result")):
    return "failed"
  if _result_is_success(agent.get("result")):
    return "done"
  if _result_is_stopped(agent.get("result")) or agent.get("interrupted"):
    return "stopped"
  if agent.get("final_report_terminal") is True:
    return "done"
  return "unknown"


def _next_from_handback(handback: dict) -> str:
  if not isinstance(handback, dict):
    return ""
  if handback.get("note"):
    return clip_line(str(handback["note"]), 280)
  actions = handback.get("actions") or []
  if actions:
    action = actions[0]
    tool = str(action.get("tool") or "tool").lower()
    target = str(action.get("target") or "").strip()
    return clip_line(f"Next, the chat used {tool}{' on ' + target if target else ''}.", 280)
  return ""


def _block_evidence(helper: dict, provider: str) -> dict:
  report = helper.get("_full_outcome") or ""
  is_async = bool(helper.get("is_async")) or _is_async_ack(report)
  state = "running" if is_async else _canonical_state(helper.get("status"))
  # Older chat caches can call a tool block "finished" even when its payload is
  # plainly the helper's next intended action. Keep that cached bookkeeping
  # from overriding a stopped trace during evidence reconciliation.
  procedural_report = _looks_progress_report(report)
  progress_demoted = state == "done" and procedural_report
  if progress_demoted:
    state = "stopped"
  return {
    "agent_id": str(helper["agent_id"]),
    "agent_type": helper.get("agent_type") or "",
    "ask": _plain_ask(helper.get("description"), helper.get("_brief_full")),
    "brief_full": helper.get("_brief_full") or "",
    # Re-detect the launch envelope here as well as at ingest so state cached by
    # an older parser is corrected without waiting for the chat to be rescanned.
    "state": state,
    "report_full": "" if is_async else report,
    "tools": [], "next": _next_from_handback(helper.get("_handback") or {}),
    "provider": provider, "origin": "block", "ts": None, "depth": None,
    "parent_agent_id": "main",
    "started_at": _coerce_iso(helper.get("_spawned_at")),
    "started_time_quality": ("observed" if helper.get("_spawned_at") is not None
                             else "unknown"),
    "ended_at": None, "ended_time_quality": "unknown",
    "last_activity_at": _coerce_iso(helper.get("_spawned_at")),
    # A cached "finished" block containing only procedural next steps proves
    # neither completion nor an explicit stop. Keep its display state
    # conservative while avoiding a false owner-attention alarm.
    "lifecycle_state": (
      "unknown"
      if state == "running" or (state == "stopped" and procedural_report)
      else state
    ),
  }


def _delivery_sentence(text: Optional[str]) -> Optional[str]:
  raw = scrub(str(text or "")).strip()
  if not raw:
    return None
  sentences = [clip_line(s, OUTCOME_CAP) for s in
               re.split(r"(?<=[.!?])\s+|\n+", raw) if s.strip()]
  for sentence in sentences:
    cleaned = re.sub(r"^[#>*\-\d.\s]+", "", sentence).strip()
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    empty_lead = re.match(
      r"^(?:done[.!]?|here(?:'|’)s (?:what (?:changed|i changed)|what i did):?)$",
      cleaned, re.I)
    if (_DELIVERY_START.match(cleaned) and not _PROCEDURAL_START.match(cleaned)
        and not empty_lead and not re.search(r"(?:/data/|/app/)", cleaned)):
      cleaned = cleaned.rstrip().removesuffix(":").rstrip()
      if not cleaned:
        continue
      return cleaned[:1].upper() + cleaned[1:]
  return None


def _plain_result(report: str, state: str) -> str:
  if state == "failed":
    return "A failure was recorded; the technical report has the original details."
  if not report:
    return ""
  delivery = _delivery_sentence(report)
  if delivery:
    return delivery
  for part in re.split(r"(?<=[.!?])\s+|\n+", report):
    line = clip_line(part, 240)
    if line and not _PROCEDURAL_START.match(line):
      return line
  return ""


def _merge_evidence(records: list[dict], provider: str) -> dict:
  state = _resolved_state(r.get("state", "stopped") for r in records)
  # Prefer a report attached to the resolved terminal state, then downstream
  # trace words, then any recorded report. Async acknowledgements were removed
  # at parse time and can never masquerade as a report here.
  reports = [r for r in records if r.get("report_full")]
  reports.sort(key=lambda r: (
    r.get("state") == state, r.get("origin") == "trace",
    len(r.get("report_full") or "")), reverse=True)
  report = reports[0]["report_full"] if reports else ""
  briefs = [r.get("brief_full") or "" for r in records]
  asks = [r.get("ask") or "" for r in records
          if r.get("ask") and r.get("ask") != "No brief was recorded"]
  types = [r.get("agent_type") or "" for r in records if r.get("agent_type")]
  tools = [tool for r in records for tool in r.get("tools", [])]
  next_lines = [r.get("next") or "" for r in records if r.get("next")]
  depths = [int(r["depth"]) for r in records
            if isinstance(r.get("depth"), int) and r["depth"] > 0]
  kind, name = _kind_and_name(types[0] if types else "", provider)
  did = _did_from_tools(tools)
  def _best_time(field: str, quality_field: str) -> tuple[Optional[str], str]:
    rank = {"unknown": 0, "estimated": 1, "observed": 2, "exact": 3}
    candidates = [(r.get(field), str(r.get(quality_field) or "unknown"))
                  for r in records if r.get(field)]
    if not candidates:
      return None, "unknown"
    return max(candidates, key=lambda item: rank.get(item[1], 0))
  started_at, started_quality = _best_time("started_at", "started_time_quality")
  ended_at, ended_quality = _best_time("ended_at", "ended_time_quality")
  lifecycle_states = [str(r.get("lifecycle_state") or "unknown") for r in records]
  lifecycle_state = _resolved_timeline_state(lifecycle_states)
  parents = [str(r["parent_agent_id"]) for r in records if r.get("parent_agent_id")]
  return {
    "agent_id": records[0]["agent_id"], "kind": kind, "name": name,
    "ask": asks[0] if asks else "No brief was recorded",
    "state": state, "acts": _acts_from_did(did),
    "result": _plain_result(report, state),
    "brief_full": max(briefs, key=len) if briefs else "",
    "depth": max(depths) if depths else 1,
    "tappable": True, "did": did, "report_full": report,
    "next": next_lines[-1] if next_lines else "",
    "commands": _commands_from_tools(tools), "_tools": tools,
    "ts": next((r.get("ts") for r in records if r.get("ts")), None),
    "provider": provider, "parent_agent_id": parents[0] if parents else None,
    "started_at": started_at, "started_time_quality": started_quality,
    "ended_at": ended_at, "ended_time_quality": ended_quality,
    "last_activity_at": next((r.get("last_activity_at") for r in records
                              if r.get("last_activity_at")), None),
    "lifecycle_state": lifecycle_state,
  }


def _resolved_timeline_state(states: Iterable[str]) -> str:
  values = {str(state or "unknown").lower() for state in states}
  if "failed" in values:
    return "failed"
  if "done" in values:
    return "done"
  if values & {"stopped", "cancelled", "interrupted"}:
    return "stopped"
  if "running" in values:
    return "running"
  return "unknown"


def _owner_noun(parts: Iterable[str]) -> str:
  text = "\n".join(str(part or "") for part in parts)
  apps: set[str] = set()
  for slug in re.findall(r"/data/apps/([A-Za-z0-9_-]+)", text):
    noun = _APP_NOUNS.get(slug.lower())
    if noun:
      apps.add(noun)
    elif not slug.isdigit() and slug.lower() not in ("shared", "state"):
      apps.add(slug.replace("_", "-").replace("-", " ").title())
  for app_id in re.findall(r"(?:/api/apps/|/data/compiled/app-)(\d+)", text):
    if app_id in _APP_NOUNS:
      apps.add(_APP_NOUNS[app_id])
  if len(apps) > 1:
    return f"{len(apps)} mini-apps"
  if apps:
    return next(iter(apps))
  low = text.lower()
  if "/data/shell/" in low:
    return "the chat UI"
  if "theme.css" in low or "/data/shared/" in low:
    return "mini-app styling"
  return "the platform"


def _request_noun(request: str) -> Optional[str]:
  """A high-confidence app/site name for a not-yet-created source directory.

  Keep this deliberately narrow: `building Name — description` is a naming
  construction, while a generic request such as `build a period tracking app`
  is a description and should not be promoted into a guessed product name.
  """
  text = scrub(request).strip()
  hit = re.search(
    r"\b(?:building|creating)\s+([A-ZÀ-ÖØ-Þ][^—–\n]{1,60}?)\s+[—–]\s+",
    text)
  if not hit:
    return None
  noun = clip_line(hit.group(1).strip(" \t.,:;\"'“”‘’"), 80)
  return noun or None


def _has_change(tools: list[dict]) -> bool:
  for tool in tools:
    name = str(tool.get("tool") or "").lower()
    detail = str(tool.get("input") or "")
    if name in ("edit", "multiedit", "write", "apply_patch", "applypatch", "create_file"):
      return True
    if re.search(r"(?:curl\b[^\n]*\s-X\s+(?:PATCH|POST|PUT|DELETE)\b|\brm\s|\bmv\s|\bcp\s|sed\s+-i\b)", detail, re.I):
      return True
  return False


def _turn_verb(tools: list[dict]) -> str:
  names = {str(t.get("tool") or "").lower() for t in tools}
  if names & {"write", "create_file", "write_file"} and not names & {"edit", "multiedit", "apply_patch"}:
    return "Built"
  if names & {"edit", "multiedit", "write", "apply_patch", "applypatch", "create_file"}:
    return "Updated"
  return "Investigated"


def _verification_signal(tools: list[dict], original: str) -> str:
  signal = "none"
  for tool in tools:
    status = str(tool.get("status") or "").lower()
    command = str(tool.get("input") or "")
    output = str(tool.get("output") or "")
    if status in ("failed", "error"):
      signal = "failed"
    elif _VERIFY_COMMAND.search(command) and _NEGATIVE_EVIDENCE.search(output):
      signal = "failed"
    elif _VERIFY_COMMAND.search(command) and _POSITIVE_EVIDENCE.search(output):
      signal = "confirmed"
  if signal != "none":
    return signal
  if (_POSITIVE_EVIDENCE.search(original)
      and not re.search(r"\b(?:should|might|could|hopefully|seems?|appears?)\b", original, re.I)):
    return "confirmed"
  return "none"


def _compact_area_evidence(parts: Iterable[str]) -> list[str]:
  """Retain only path/ID markers that `_owner_noun` understands.

  Raw tool inputs dominated the persistent state even though document assembly
  only used a few app/shell/theme identifiers from them. These compact markers
  preserve the same area classification without retaining command payloads.
  """
  text = "\n".join(str(part or "") for part in parts)
  markers: list[str] = []
  for slug in re.findall(r"/data/apps/([A-Za-z0-9_-]+)", text):
    marker = f"/data/apps/{slug}"
    if marker not in markers:
      markers.append(marker)
  for app_id in re.findall(r"(?:/api/apps/|/data/compiled/app-)(\d+)", text):
    marker = f"/api/apps/{app_id}"
    if marker not in markers:
      markers.append(marker)
  low = text.lower()
  if "/data/shell/" in low:
    markers.append("/data/shell/")
  if "theme.css" in low or "/data/shared/" in low:
    markers.append("theme.css")
  return markers[:32]


def _compact_turn_facts(tools: list[dict], original: str) -> dict:
  return {
    "area_evidence": _compact_area_evidence(
      tool.get("input", "") for tool in tools if isinstance(tool, dict)),
    "verb": _turn_verb(tools),
    "changed": _has_change(tools),
    "verification": _verification_signal(tools, original),
  }


def _compact_chat_turn(raw: dict, keep_request: bool = False) -> dict:
  """Migrate one cached v2 owner turn to the compact v3 state shape."""
  original = _cap_report(str(raw.get("_original") or ""))
  tools = raw.get("_tools") if isinstance(raw.get("_tools"), list) else []
  facts = raw.get("_facts") if isinstance(raw.get("_facts"), dict) else None
  if facts is None:
    facts = _compact_turn_facts(tools, original)
  return {
    "ts": raw.get("ts"),
    "_agent_ids": [str(aid) for aid in (raw.get("_agent_ids") or []) if aid],
    "_facts": facts,
    "_original": original,
    "_first_request": (clip_markdown(str(raw.get("_first_request") or ""), FULL_PROMPT_CAP) or ""
                       if keep_request else ""),
  }


def _compact_chat_turns_state(state) -> dict[str, list[dict]]:
  """Compact every cached chat without discarding attribution or chronology."""
  if not isinstance(state, dict):
    return {}
  compacted: dict[str, list[dict]] = {}
  for chat_id, rows in state.items():
    if not isinstance(rows, list):
      continue
    first_request = next((str(row.get("_first_request") or "") for row in rows
                          if isinstance(row, dict) and row.get("_first_request")), "")
    out = [_compact_chat_turn(row) for row in rows if isinstance(row, dict)]
    if out and first_request:
      out[0]["_first_request"] = clip_markdown(first_request, FULL_PROMPT_CAP) or ""
    compacted[str(chat_id)] = out[-MAX_CHAT_TURNS:]
  return compacted


def _clean_chat_helpers_state(state) -> dict[str, list[dict]]:
  """Drop cached renderer placeholders that never represented a helper."""
  if not isinstance(state, dict):
    return {}
  cleaned: dict[str, list[dict]] = {}
  for chat_id, rows in state.items():
    if not isinstance(rows, list):
      continue
    keep = []
    for row in rows:
      if not isinstance(row, dict):
        continue
      generated_id = str(row.get("agent_id") or "").startswith(("call_", "blk"))
      placeholder = (generated_id and not row.get("description")
                     and not row.get("_brief_full") and not row.get("_full_outcome"))
      if not placeholder:
        keep.append(row)
    if keep:
      cleaned[str(chat_id)] = keep
  return cleaned


def _hedges_result(original: str) -> bool:
  """Whether the agent's own closing words express positive doubt.

  Missing test evidence is deliberately not doubt. This only examines the
  agent-authored report, never the user's request or tool output, so words such
  as "may" in a brief cannot turn an otherwise ordinary edit amber.
  """
  return bool(_HEDGE_EVIDENCE.search(original))


def _turn_stopped(original: str) -> bool:
  """An explicit owner-turn stop is doubt even without a helper lifecycle."""
  return bool(_STOPPED_EVIDENCE.search(original))


def _found_cause(original: str) -> bool:
  return bool(re.search(
    r"\b(?:root cause|the (?:issue|problem|cause) (?:is|was)|caused by|confirmed\s+.{0,30}\b(?:issue|problem))\b",
    original, re.I))


def _coerce_iso(value) -> Optional[str]:
  if isinstance(value, (int, float)):
    epoch = float(value) / 1000 if value > 10_000_000_000 else float(value)
    return _epoch_to_iso(epoch)
  if isinstance(value, str) and value.strip():
    return value.strip()
  return None


def _build_v3_turn(raw: dict, helpers: dict[str, dict],
                   request_hint: str = "") -> dict:
  subs = [helpers[aid] for aid in raw.get("_agent_ids", []) if aid in helpers]
  # Preserve one card per helper in a turn even when an async ack and later result
  # block both named the same agent_id.
  subs = list({sub["agent_id"]: sub for sub in subs}.values())
  raw_tools = list(raw.get("_tools", []))
  sub_tools = [tool for sub in subs for tool in sub.get("_tools", [])]
  original = _cap_report(str(raw.get("_original") or ""))
  facts = raw.get("_facts") if isinstance(raw.get("_facts"), dict) else None
  if facts is not None:
    area = _owner_noun(
      list(facts.get("area_evidence") or []) +
      [t.get("input", "") for t in sub_tools] +
      [sub.get("brief_full", "") for sub in subs])
    sub_changed = _has_change(sub_tools)
    changed = bool(facts.get("changed")) or sub_changed
    sub_verb = _turn_verb(sub_tools) if sub_tools else "Investigated"
    verbs = {str(facts.get("verb") or "Investigated"), sub_verb}
    verb = "Updated" if "Updated" in verbs else "Built" if "Built" in verbs else "Investigated"
    verification = str(facts.get("verification") or "none")
    sub_verification = _verification_signal(sub_tools, original)
    if "failed" in (verification, sub_verification):
      verification = "failed"
    elif "confirmed" in (verification, sub_verification):
      verification = "confirmed"
  else:
    tools = raw_tools + sub_tools
    area = _owner_noun(
      [t.get("input", "") for t in tools] +
      [sub.get("brief_full", "") for sub in subs])
    changed = _has_change(tools)
    verb = _turn_verb(tools)
    verification = _verification_signal(tools, original)
  if area == "the platform":
    area = (_request_noun(str(raw.get("_first_request") or ""))
            or _request_noun(request_hint) or area)
  neutral = f"{verb} {area}"
  states = [sub["state"] for sub in subs]
  lifecycle_states = [sub.get("lifecycle_state", "unknown") for sub in subs]
  delivery = _delivery_sentence(original)

  flag = None
  # A helper failure is evidence about that helper, not automatically an owner
  # decision. When the main agent subsequently delivered the requested result,
  # keep the failure visible in the timeline without turning the whole chat
  # amber. Verification failure and explicit uncertainty still win.
  if verification == "failed":
    status, result = "attention", "not confirmed"
    flag = f"The recorded check for {area} failed, so the claimed result was not confirmed."
  elif _turn_stopped(original):
    status, result = "attention", "stopped"
    flag = "The recorded work stopped without a completion result."
  elif _hedges_result(original):
    status, result = "attention", "not confirmed"
    flag = f"The recorded report for {area} hedged the result, so it was not confirmed."
  elif "failed" in states and not delivery:
    status, result = "attention", "couldn't complete"
    flag = "A helper failed before returning a usable result."
  elif "running" in states:
    status, result = "running", "in progress"
  elif "stopped" in lifecycle_states and not delivery:
    status, result = "attention", "stopped"
    flag = "The recorded work stopped without a completion result."
  else:
    status = "done"
    result = "found the cause" if not changed and _found_cause(original) else "done"

  # A delivery claim is only a hero line when the evidence supports `done`.
  # Attention/running turns stay neutral and put the caveat in `flag`.
  outcome = delivery if status == "done" and delivery else neutral

  # A finished fleet whose journal launched more helpers than ever reported
  # gets an honest, display-only completeness note. Deliberately NOT a flag:
  # a delivered turn stays out of "Needs you"; the owner just gets to tell a
  # complete answer from a partial one.
  note = None
  silent = raw.get("_silent_helpers")
  if silent and status != "running":
    launched = int(raw.get("_launched_helpers") or 0) or len(subs) or int(silent)
    unit = "helper" if launched == 1 else "helpers"
    note = (f"{silent} of {launched} {unit} never reported a result, "
            "so this outcome may reflect partial work.")

  public_subs = [{
    "agent_id": sub["agent_id"], "kind": sub["kind"], "name": sub["name"],
    "ask": sub["ask"], "state": sub["state"], "depth": sub["depth"],
    "prompt_available": bool(sub.get("brief_full")),
  } for sub in subs]
  return {
    "outcome": clip_line(outcome, OUTCOME_CAP), "area": area,
    "result": result, "status": status, "flag": flag, "note": note,
    "ts": _coerce_iso(raw.get("ts")), "subs": public_subs,
  }


def _trace_turn(run: dict, agents: list[dict]) -> dict:
  tools = [tool for agent in agents for tool in agent.get("tools", [])]
  reports = [agent.get("report_full") or "" for agent in agents if agent.get("report_full")]
  original = reports[-1] if reports else ""
  turn = {
    "ts": run.get("started_at") or next((a.get("ts") for a in agents if a.get("ts")), None),
    "_agent_ids": [a["agent_id"] for a in agents],
    "_facts": _compact_turn_facts(tools, original), "_original": original,
    "_first_request": "",
  }
  if (run.get("kind") == "workflow" and run.get("journal_counted_from_start")
      and run.get("journal_caught_up")):
    # Launched-versus-reported from the run journal only: it records every
    # helper the runtime launched and every result it consumed, independently
    # of transcript survival or display retention. The two integrity guards
    # (counted from byte 0, read through EOF) make a claimed gap trustworthy;
    # without them — a cursor that pre-dates the counters, or a partial read —
    # we say nothing rather than risk calling complete work partial.
    journal_started = int(run.get("journal_started") or 0)
    launched = max(journal_started, len(agents))
    reported = min(int(run.get("journal_resulted") or 0), launched)
    silent = max(0, launched - reported) if journal_started else 0
    if silent and not any(a.get("state") == "running" for a in agents):
      turn["_silent_helpers"] = silent
      turn["_launched_helpers"] = launched
  return turn


def _title_is_raw(title: str) -> bool:
  value = title.strip()
  if not value or value.lower() in ("new chat", "untitled chat"):
    return True
  if re.match(r"^(?:could|can|would|will) you\b|^please\b|^i (?:need|want|can't|am)\b|^so\b", value, re.I):
    return True
  if "```" in value or value.startswith("<"):
    return True
  return 36 <= len(value) <= 44 and value[-1].isalnum()


def _title_from_request(request: str, area: str, changed: bool) -> str:
  clean = clip_line(request, 180)
  clean = clean.split("```", 1)[0].strip()
  clean = re.sub(
    r"^(?:(?:could|can|would|will) you(?: please)?|please)\s+", "", clean,
    flags=re.I).strip()
  clean = re.sub(r"^(?:i (?:want|need)(?: you)? to|so)\s+", "", clean,
                 flags=re.I).strip()
  clean = re.sub(r"^(?:i(?:'|’)m|i am)\s+", "", clean, flags=re.I).strip()
  clean = re.sub(r"^build me\s+", "Build ", clean, flags=re.I).strip()
  clean = re.split(r"[.!?\n]", clean)[0].strip()
  words = clean.split()
  if words:
    candidate = " ".join(words[:10]).rstrip(" ,;:-")
    return candidate[:1].upper() + candidate[1:]
  if area == "the platform":
    return "Background work"
  return f"{area} {'update' if changed else 'investigation'}"


def _chat_status(turns: list[dict]) -> tuple[str, str]:
  if not turns:
    return "done", "done"
  running = [turn for turn in turns if turn["status"] == "running"]
  if running:
    return "running", "in progress"
  # The journal entry describes where the chat ended. Older unverified turns
  # stay visible in drill-in but do not keep a chat in "Needs you" after a later
  # turn resolved the work.
  return turns[-1]["status"], turns[-1]["result"]


def _event_time_key(event: dict) -> tuple[float, int, str]:
  occurred = _iso_to_epoch(event.get("occurred_at"))
  observed = _iso_to_epoch(event.get("observed_at"))
  return (occurred if occurred is not None else
          observed if observed is not None else float("inf"),
          int(event.get("source_order") or event.get("id") or 0),
          str(event.get("event_id") or ""))


def _ordered_timeline_events(events: list[dict], parent_by_agent: dict[str, Optional[str]]) -> list[dict]:
  """Stable ``O(E log E)`` causal order for out-of-order observations."""
  unique: dict[str, dict] = {}
  semantic: set[tuple] = set()
  for event in events:
    event_id = str(event.get("event_id") or "")
    if not event_id or event_id in unique:
      continue
    sig = (event.get("subject_agent_id"), event.get("type"), event.get("state"),
           event.get("occurred_at"), event.get("source_event_id"),
           event.get("summary"))
    if sig in semantic:
      continue
    semantic.add(sig)
    unique[event_id] = event
  rows = list(unique.values())
  by_agent: dict[str, list[int]] = {}
  for i, row in enumerate(rows):
    if row.get("subject_agent_id"):
      by_agent.setdefault(str(row["subject_agent_id"]), []).append(i)
  deps: list[set[int]] = [set() for _ in rows]
  dependents: list[set[int]] = [set() for _ in rows]
  stage = {"agent_spawned": 0, "agent_started": 1, "agent_terminal": 2}
  for indexes in by_agent.values():
    # One chain is enough to enforce stage causality; connecting every pair is
    # quadratic and adds no ordering information.
    chain = sorted(indexes, key=lambda i: (
      stage.get(rows[i].get("type"), 1), _event_time_key(rows[i])))
    for earlier, later in zip(chain, chain[1:]):
      deps[later].add(earlier)
  for child, parent in parent_by_agent.items():
    if not parent or parent == "main" or parent not in by_agent or child not in by_agent:
      continue
    parent_starts = [i for i in by_agent[parent]
                     if rows[i].get("type") in ("agent_spawned", "agent_started")]
    child_starts = [i for i in by_agent[child]
                    if rows[i].get("type") in ("agent_spawned", "agent_started")]
    if parent_starts:
      parent_start = min(parent_starts, key=lambda i: _event_time_key(rows[i]))
      for child_start in child_starts:
        deps[child_start].add(parent_start)
  for later, requirements in enumerate(deps):
    for earlier in requirements:
      dependents[earlier].add(later)
  ordered: list[dict] = []
  indegree = [len(requirements) for requirements in deps]
  ready = [(_event_time_key(rows[i]), i) for i, degree in enumerate(indegree)
           if degree == 0]
  heapq.heapify(ready)
  emitted: set[int] = set()
  while ready:
    _, chosen = heapq.heappop(ready)
    if chosen in emitted:
      continue
    emitted.add(chosen)
    public = dict(rows[chosen])
    public["order"] = len(ordered)
    public.pop("id", None)
    public.pop("source_order", None)
    public.pop("source_event_id", None)
    ordered.append(public)
    for later in dependents[chosen]:
      indegree[later] -= 1
      if indegree[later] == 0:
        heapq.heappush(ready, (_event_time_key(rows[later]), later))
  if len(emitted) != len(rows):
    # Corrupt/cyclic ancestry cannot wedge the digest. Emit the remainder in a
    # deterministic time order and retain the fact that it existed.
    for chosen in sorted(set(range(len(rows))) - emitted,
                         key=lambda i: _event_time_key(rows[i])):
      public = dict(rows[chosen])
      public["order"] = len(ordered)
      public.pop("id", None)
      public.pop("source_order", None)
      public.pop("source_event_id", None)
      ordered.append(public)
  return ordered


def _timeline_event(event_id: str, event_type: str, subject: str,
                    actor: Optional[str], state: Optional[str],
                    occurred_at: Optional[str], observed_at: Optional[str],
                    quality: str, summary: str = "", source_order: int = 0,
                    source_event_id: Optional[str] = None,
                    chat_run_id: Optional[str] = None) -> dict:
  return {
    "event_id": event_id, "type": event_type,
    "occurred_at": occurred_at, "observed_at": observed_at,
    "time_quality": quality if quality in _TIME_QUALITIES else "unknown",
    "actor_agent_id": actor, "subject_agent_id": subject,
    "state": state, "summary": clip_line(summary, OUTCOME_CAP),
    "chat_run_id": chat_run_id, "source_order": source_order,
    "source_event_id": source_event_id,
  }


def _platform_overlay(merged: dict[str, dict], events: list[dict], provider: str
                      ) -> dict[str, str]:
  """Apply authoritative platform lifecycle state to trace metadata in-place.

  Returns provider/native id -> public opaque id aliases used to rewrite owner
  turn references. Prompt/report metadata remains trace-derived; only lifecycle
  identity, parentage, timing and state are replaced.
  """
  aliases: dict[str, str] = {}
  grouped: dict[str, list[dict]] = {}
  for event in events:
    grouped.setdefault(event["agent_id"], []).append(event)
  for public_id, agent_events in grouped.items():
    native_ids = [e.get("provider_agent_id") for e in agent_events
                  if e.get("provider_agent_id")]
    native = native_ids[0] if native_ids else public_id
    helper = merged.get(native) or merged.get(public_id)
    if (helper is not None and helper.get("_platform_activation_id")
        and helper.get("_platform_activation_id") != public_id):
      helper = dict(helper)
      merged[f"platform:{public_id}"] = helper
    if helper is None:
      agent_type = next((e.get("agent_type") for e in agent_events
                         if e.get("agent_type")), "")
      kind, name = _kind_and_name(agent_type, provider)
      helper = {
        "agent_id": public_id, "kind": kind, "name": name,
        "ask": next((e.get("summary") for e in agent_events if e.get("summary")),
                    "No brief was recorded"),
        "state": "unknown", "brief_full": "", "depth": 1,
        "result": "", "_tools": [], "provider": provider,
        "started_at": None, "started_time_quality": "unknown",
        "ended_at": None, "ended_time_quality": "unknown",
        "last_activity_at": None, "parent_agent_id": None,
        "prompt_available": False,
      }
      merged[f"platform:{public_id}"] = helper
    helper["agent_id"] = public_id
    helper["_platform_activation_id"] = public_id
    helper["chat_run_id"] = next((event.get("chat_run_id") for event in
                                   reversed(sorted(agent_events,
                                                   key=lambda row: row["id"]))
                                   if event.get("chat_run_id")), None)
    aliases[str(native)] = public_id
    terminal_seen = False
    for event in sorted(agent_events, key=lambda row: row["id"]):
      if event.get("parent_agent_id") and event["parent_agent_id"] != public_id:
        helper["parent_agent_id"] = event["parent_agent_id"]
      if event.get("summary"):
        helper["ask"] = helper.get("ask") or event["summary"]
      when = event.get("occurred_at") or event.get("observed_at")
      if event["type"] in ("agent_spawned", "agent_started"):
        if not helper.get("started_at") or event["time_quality"] == "exact":
          helper["started_at"] = when
          helper["started_time_quality"] = event["time_quality"]
        # Start-only platform evidence is not a contradiction of an explicit
        # trace terminal. This matters for Codex, whose current SDK persists
        # starts/interruption but has no positive completion notification.
        if (not terminal_seen
            and helper.get("lifecycle_state") not in ("done", "failed", "stopped")):
          helper["lifecycle_state"] = helper["state"] = "running"
      elif event["type"] == "agent_terminal":
        terminal_seen = True
        helper["lifecycle_state"] = helper["state"] = event["state"]
        helper["ended_at"] = when
        helper["ended_time_quality"] = event["time_quality"]
        if event.get("summary"):
          helper["result"] = event["summary"]
      helper["last_activity_at"] = when or event.get("observed_at")
    terminals = [event for event in agent_events
                 if event.get("type") == "agent_terminal"]
    if terminals:
      resolved_terminal = _resolved_timeline_state(
        event.get("state") for event in terminals)
      helper["lifecycle_state"] = helper["state"] = resolved_terminal
      matching = [event for event in terminals
                  if event.get("state") == resolved_terminal] or terminals
      chosen_terminal = max(matching, key=lambda row: row.get("id", 0))
      helper["ended_at"] = (chosen_terminal.get("occurred_at")
                            or chosen_terminal.get("observed_at"))
      helper["ended_time_quality"] = chosen_terminal.get("time_quality") or "unknown"
      if chosen_terminal.get("summary"):
        helper["result"] = chosen_terminal["summary"]
    started_epoch = _iso_to_epoch(helper.get("started_at"))
    ended_epoch = _iso_to_epoch(helper.get("ended_at"))
    if (started_epoch is not None and ended_epoch is not None
        and started_epoch > ended_epoch):
      # Preserve the raw events, but do not publish an impossible aggregate
      # duration when provider clocks or replay order conflict.
      helper["started_at"] = None
      helper["started_time_quality"] = "unknown"
      helper["timing_conflict"] = True
  for helper in merged.values():
    parent = helper.get("parent_agent_id")
    if parent in aliases:
      helper["parent_agent_id"] = aliases[parent]
  return aliases


def _sanitize_timeline_parents(agents: list[dict]) -> dict[str, Optional[str]]:
  ids = {str(agent["agent_id"]) for agent in agents}
  parent_map: dict[str, Optional[str]] = {}
  for agent in agents:
    aid = str(agent["agent_id"])
    parent = str(agent.get("parent_agent_id") or "") or None
    if parent not in ids and parent != "main":
      parent = None
    if parent == aid:
      parent = None
    parent_map[aid] = parent
  # Break cycles without inventing a replacement edge.
  for aid in list(parent_map):
    seen: set[str] = set()
    cur = aid
    while cur and cur != "main" and cur in parent_map:
      if cur in seen:
        parent_map[aid] = None
        break
      seen.add(cur)
      cur = parent_map.get(cur)
  return parent_map


def _retain_recent_helpers(merged: dict[str, dict]) -> tuple[dict[str, dict], int]:
  """Bound one chat by latest observed helper activity, deterministically."""
  if len(merged) <= MAX_TIMELINE_AGENTS:
    return merged, 0
  ranked: list[tuple[float, int, str]] = []
  for insertion, (key, helper) in enumerate(merged.items()):
    timestamps = (
      helper.get("last_activity_at"), helper.get("ended_at"),
      helper.get("started_at"), helper.get("ts"),
    )
    activity = max((_iso_to_epoch(value) for value in timestamps), default=None,
                   key=lambda value: value if value is not None else float("-inf"))
    ranked.append((activity if activity is not None else float("-inf"), insertion, key))
  keep = {key for _, _, key in sorted(ranked)[-MAX_TIMELINE_AGENTS:]}
  return ({key: helper for key, helper in merged.items() if key in keep},
          len(merged) - len(keep))


def _compact_public_timeline_events(events: list[dict]) -> tuple[list[dict], int]:
  """Keep only the milestones needed by the skim-first timeline.

  Provider wrappers may report the same activation many times. The visual
  contract intentionally needs at most one spawn, one start and the resolved
  terminal per helper, plus the already-bounded owner checkpoints.
  """
  checkpoints = [event for event in events if event.get("type") == "main_checkpoint"]
  by_agent: dict[str, list[dict]] = {}
  for event in events:
    if event.get("type") == "main_checkpoint":
      continue
    subject = str(event.get("subject_agent_id") or "")
    if subject:
      by_agent.setdefault(subject, []).append(event)
  retained = checkpoints[-40:]
  for agent_events in by_agent.values():
    for milestone in ("agent_spawned", "agent_started"):
      candidates = [event for event in agent_events if event.get("type") == milestone]
      if candidates:
        retained.append(min(candidates, key=_event_time_key))
    terminals = [event for event in agent_events
                 if event.get("type") == "agent_terminal"]
    if terminals:
      resolved = _resolved_timeline_state(
        event.get("state") for event in terminals)
      matching = [event for event in terminals if event.get("state") == resolved]
      retained.append(max(matching or terminals, key=lambda event: (
        int(event.get("source_order") or event.get("id") or 0),
        _event_time_key(event))))
  if len(retained) > MAX_TIMELINE_EVENTS_PER_CHAT:
    retained = sorted(retained, key=_event_time_key)[-MAX_TIMELINE_EVENTS_PER_CHAT:]
  return retained, max(0, len(events) - len(retained))


def _build_timeline(chat_id: str, provider: str, merged: dict[str, dict],
                    platform_events: list[dict], platform_runs: list[dict],
                    turns: list[dict], agents_omitted: int = 0,
                    events_omitted: int = 0) -> dict:
  # Outcome-only historical records have no assignment to distinguish them
  # from their siblings. They add repeated "No brief was recorded" lanes but
  # cannot answer the owner's primary question: what was this helper doing?
  # Keep unresolved records for honesty; omit only resolved, taskless rows and
  # report the omission explicitly in retention metadata.
  eligible_helpers = [
    helper for helper in merged.values()
    if ((helper.get("ask") and helper.get("ask") != "No brief was recorded")
        or helper.get("brief_full")
        or helper.get("lifecycle_state") in ("failed", "stopped", "running"))
  ]
  # Only an identical full prompt can establish the same historical
  # assignment. Short summaries are deliberately lossy and must never be used
  # as identity: many ensemble prompts share context but differ in a trailing
  # "YOUR AREA" section. Sequential failed/stopped/unknown launches of the exact
  # prompt are attempts; overlapping launches and any launch after a completed
  # task stay visible as distinct helpers.
  grouped_helpers: dict[tuple[str, str], list[tuple[int, dict]]] = {}
  for insertion, helper in enumerate(eligible_helpers):
    prompt = re.sub(
      r"\s+", " ", str(helper.get("brief_full") or "").strip()).casefold()
    key = (str(helper.get("parent_agent_id") or "main"), prompt)
    if not prompt:
      key = (str(helper.get("agent_id")), "")
    grouped_helpers.setdefault(key, []).append((insertion, helper))

  def _helper_epoch(helper: dict, *fields: str) -> Optional[float]:
    values = [_iso_to_epoch(helper.get(field)) for field in fields]
    finite = [value for value in values if value is not None]
    return max(finite) if finite else None

  logical_attempts: list[list[tuple[int, dict]]] = []
  for assignment_rows in grouped_helpers.values():
    def _start_key(row: tuple[int, dict]) -> tuple[float, int]:
      value = _iso_to_epoch(row[1].get("started_at"))
      if value is None:
        value = _helper_epoch(row[1], "last_activity_at", "ended_at")
      return (value if value is not None else float("inf"), row[0])

    ordered = sorted(assignment_rows, key=_start_key)
    partitions: list[list[tuple[int, dict]]] = []
    for row in ordered:
      if not partitions:
        partitions.append([row])
        continue
      previous = partitions[-1][-1][1]
      current = row[1]
      previous_state = str(previous.get("lifecycle_state") or "unknown")
      previous_end = _helper_epoch(
        previous, "last_activity_at", "ended_at", "started_at")
      current_start = _iso_to_epoch(current.get("started_at"))
      sequential_retry = (
        previous_state in ("failed", "stopped", "unknown")
        and previous_end is not None
        and current_start is not None
        and current_start >= previous_end
      )
      if sequential_retry:
        partitions[-1].append(row)
      else:
        partitions.append([row])
    logical_attempts.extend(partitions)

  display_helpers = []
  for attempts in logical_attempts:
    def _activity_key(row: tuple[int, dict]) -> tuple[float, int]:
      value = _helper_epoch(
        row[1], "last_activity_at", "ended_at", "started_at")
      return (value if value is not None else float("-inf"), row[0])

    insertion, helper = max(attempts, key=_activity_key)
    public_helper = dict(helper)
    public_helper["_display_insertion"] = insertion
    public_helper["_attempt_count"] = len(attempts)
    public_helper["_attempt_states"] = dict(Counter(
      str(row[1].get("lifecycle_state") or "unknown") for row in attempts))
    display_helpers.append(public_helper)
  display_helpers.sort(key=lambda helper: helper["_display_insertion"])
  detail_omitted = max(0, len(merged) - len(display_helpers))
  display_ids = {str(helper["agent_id"]) for helper in display_helpers}
  agents: list[dict] = []
  seen: set[str] = set()
  for helper in display_helpers:
    aid = str(helper["agent_id"])
    if aid in seen:
      continue
    seen.add(aid)
    agents.append({
      "agent_id": aid, "parent_agent_id": helper.get("parent_agent_id"),
      "chat_run_id": helper.get("chat_run_id"),
      "provider": helper.get("provider") or provider,
      "kind": helper.get("kind") or "general", "name": helper.get("name") or "Helper",
      "task_summary": helper.get("ask") or "No brief was recorded",
      "state": helper.get("lifecycle_state") or "unknown",
      "prompt_available": bool(helper.get("brief_full")),
      "outcome_summary": helper.get("result") or "",
      "attempt_count": helper["_attempt_count"],
      "attempt_states": helper["_attempt_states"],
      "started_at": helper.get("started_at"), "ended_at": helper.get("ended_at"),
      "start_time_quality": helper.get("started_time_quality") or "unknown",
      "end_time_quality": helper.get("ended_time_quality") or "unknown",
      "last_activity_at": helper.get("last_activity_at"),
      "timing_conflict": bool(helper.get("timing_conflict")),
    })
  parent_map = _sanitize_timeline_parents(agents)
  for agent in agents:
    agent["parent_agent_id"] = parent_map[agent["agent_id"]]

  public_events: list[dict] = []
  visible_platform_events = [
    event for event in platform_events
    if str(event.get("agent_id") or "") in display_ids
  ]
  platform_subjects = {event["agent_id"] for event in visible_platform_events}
  for event in visible_platform_events:
    actor = event.get("parent_agent_id")
    public_events.append(_timeline_event(
      event["event_id"], event["type"], event["agent_id"], actor,
      event.get("state"), event.get("occurred_at"), event.get("observed_at"),
      event.get("time_quality") or "unknown", event.get("summary") or "",
      source_order=event.get("id") or 0,
      source_event_id=event.get("source_event_id"),
      chat_run_id=event.get("chat_run_id")))
  for helper in display_helpers:
    aid = str(helper["agent_id"])
    if aid in platform_subjects:
      continue
    parent = parent_map.get(aid)
    start = helper.get("started_at")
    start_quality = helper.get("started_time_quality") or "unknown"
    event_seed = f"trace:{chat_id}:{aid}"
    public_events.append(_timeline_event(
      event_seed + ":start", "agent_started" if start else "agent_spawned",
      aid, parent, "running" if start else None, start,
      helper.get("last_activity_at") if not start else start,
      start_quality, helper.get("ask") or ""))
    state = helper.get("lifecycle_state") or "unknown"
    if state in ("done", "failed", "stopped"):
      public_events.append(_timeline_event(
        event_seed + ":terminal", "agent_terminal", aid, aid, state,
        helper.get("ended_at"), helper.get("last_activity_at"),
        helper.get("ended_time_quality") or "unknown", helper.get("result") or ""))
  # Owner turns are first-class checkpoints on the same time axis. Keeping
  # these in the canonical event stream means every renderer gets correct
  # interleaving instead of appending a second, visually misleading sequence.
  for index, turn in enumerate(turns):
    when = turn.get("ts")
    event = _timeline_event(
      f"main:{chat_id}:checkpoint:{index}", "main_checkpoint", "main", "main",
      turn.get("status"), when, when, "exact" if when else "unknown",
      turn.get("outcome") or "", source_order=index)
    if turn.get("note"):
      event["note"] = turn["note"]
    public_events.append(event)
  main_runs = [dict(run, start_time_quality="exact" if run.get("started_at") else "unknown",
                    end_time_quality="exact" if run.get("ended_at") else "unknown")
               for run in platform_runs[-MAX_MAIN_RUNS_PER_CHAT:]]
  if not main_runs:
    first_ts = next((turn.get("ts") for turn in turns if turn.get("ts")), None)
    status, _ = _chat_status(turns)
    main_runs = [{
      "id": f"fallback:{chat_id}", "chat_id": chat_id, "provider": provider,
      "status": "running" if status == "running" else "unknown",
      "started_at": first_ts, "ended_at": None,
      "start_time_quality": "observed" if first_ts else "unknown",
      "end_time_quality": "unknown",
    }]
  public_events, compacted_events = _compact_public_timeline_events(public_events)
  return {
    "main_agent_id": "main", "main_runs": main_runs,
    "agents": agents,
    "events": _ordered_timeline_events(public_events, parent_map),
    "retention": {
      "agents_omitted": max(0, int(agents_omitted) + detail_omitted),
      "events_omitted": max(
        0, int(events_omitted)
        + len(platform_events) - len(visible_platform_events)
        + compacted_events),
    },
  }


def build_documents(model: dict, attribution: Attribution, now: float,
                    chat_helpers: Optional[dict[str, list[dict]]] = None,
                    chat_turns: Optional[dict[str, list[dict]]] = None,
                    lifecycle_events: Optional[list[dict]] = None,
                    lifecycle_runs: Optional[list[dict]] = None,
                    lifecycle_events_omitted: Optional[dict[str, int]] = None,
                    ) -> tuple[dict, dict[str, dict], dict[str, dict], dict[str, dict]]:
  """Rebuild schema-v4 journal, chat, prompt and lifecycle documents."""
  sessions = model["sessions"]
  evidence: dict[str, dict[str, list[dict]]] = {}
  synthetic_turns: dict[str, list[dict]] = {}
  providers: dict[str, str] = {}
  unlinked: dict[str, dict] = {}
  events_by_chat: dict[str, list[dict]] = {}
  runs_by_chat: dict[str, list[dict]] = {}
  visible_chat_ids = set(attribution.chats)
  for event in lifecycle_events or []:
    if (isinstance(event, dict) and event.get("chat_id")
        and str(event["chat_id"]) in visible_chat_ids):
      events_by_chat.setdefault(str(event["chat_id"]), []).append(event)
  for run in lifecycle_runs or []:
    if (isinstance(run, dict) and run.get("chat_id")
        and str(run["chat_id"]) in visible_chat_ids):
      runs_by_chat.setdefault(str(run["chat_id"]), []).append(run)

  # Group runs by resolved chat. A session with no runs but that resolves to a
  # chat contributes nothing (it is the chat's own top-level turn); a session
  # with helpers that does NOT resolve becomes an unlinked row.
  runs_by_sid: dict[str, list[dict]] = {}
  for run in model["runs"].values():
    if run["agent_keys"]:
      runs_by_sid.setdefault(run["sid"], []).append(run)

  for sid, runs in runs_by_sid.items():
    chat_id, reason = attribution.resolve(sid, sessions)
    session = sessions.get(sid, {})
    if not chat_id:
      row = unlinked.setdefault(sid, {
        "provider": session.get("provider", "claude"),
        "session_id": sid, "reason": reason,
        "last_activity_at": session.get("last_activity_at"), "helpers": 0,
      })
      row["helpers"] += sum(len(r["agent_keys"]) for r in runs)
      continue
    provider = attribution.chats.get(chat_id, {}).get("provider") or session.get("provider") or "claude"
    providers[chat_id] = provider
    for run in runs:
      run_evidence = []
      for akey in run["agent_keys"]:
        agent = model["agents"].get(akey)
        if not agent:
          continue
        item = _trace_evidence(agent, now, provider)
        evidence.setdefault(chat_id, {}).setdefault(item["agent_id"], []).append(item)
        run_evidence.append(item)
      if run_evidence:
        synthetic_turns.setdefault(chat_id, []).append(_trace_turn(run, run_evidence))

  for chat_id, helpers in (chat_helpers or {}).items():
    if chat_id not in visible_chat_ids:
      continue
    provider = attribution.chats.get(chat_id, {}).get("provider") or providers.get(chat_id) or "claude"
    providers[chat_id] = provider
    for helper in helpers:
      if not helper.get("agent_id"):
        continue
      item = _block_evidence(helper, provider)
      evidence.setdefault(chat_id, {}).setdefault(item["agent_id"], []).append(item)

  chats: dict[str, dict] = {}
  helpers_out: dict[str, dict] = {}
  used_helper_ids: dict[str, str] = {}
  all_chat_ids = ((set(evidence) | set(chat_turns or {}) | set(events_by_chat))
                  & visible_chat_ids)
  for chat_id in all_chat_ids:
    if ((chat_id not in evidence or not evidence[chat_id])
        and not events_by_chat.get(chat_id)):
      continue  # roster contains chats with background helpers, not tool-only chats
    provider = providers.get(chat_id) or attribution.chats.get(chat_id, {}).get("provider") or "claude"
    merged: dict[str, dict] = {}
    for original_id, records in evidence.get(chat_id, {}).items():
      helper = _merge_evidence(records, provider)
      public_id = original_id
      if public_id in used_helper_ids and used_helper_ids[public_id] != chat_id:
        suffix = hashlib.sha256(chat_id.encode()).hexdigest()[:6]
        public_id = f"{original_id}-{suffix}"
      used_helper_ids[public_id] = chat_id
      helper["agent_id"] = public_id
      merged[original_id] = helper

    chat_platform_events = events_by_chat.get(chat_id, [])
    _platform_overlay(merged, chat_platform_events, provider)
    merged, agents_omitted = _retain_recent_helpers(merged)
    retained_agent_ids = {str(helper["agent_id"]) for helper in merged.values()}
    retained_platform_events = [
      event for event in chat_platform_events
      if str(event.get("agent_id")) in retained_agent_ids
    ]
    events_omitted = (len(chat_platform_events) - len(retained_platform_events)
                      + max(0, int((lifecycle_events_omitted or {}).get(chat_id, 0))))

    owner_turns = list((chat_turns or {}).get(chat_id, []))
    referenced = {aid for turn in owner_turns for aid in turn.get("_agent_ids", [])}
    raw_turns = owner_turns + [turn for turn in synthetic_turns.get(chat_id, [])
                               if any(aid not in referenced for aid in turn.get("_agent_ids", []))]
    if not raw_turns and merged:
      first_event = min(events_by_chat.get(chat_id, []),
                        key=lambda row: row.get("id", 0), default={})
      raw_turns = [{
        "ts": first_event.get("occurred_at") or first_event.get("observed_at"),
        "_agent_ids": list(merged.keys()),
        "_facts": {"area_evidence": [], "verb": "Investigated",
                   "changed": False, "verification": "none"},
        "_original": "", "_first_request": "",
      }]
    stored_title = str(attribution.chats.get(chat_id, {}).get("title") or "Untitled chat")
    turns = [_build_v3_turn(raw, merged, stored_title) for raw in raw_turns]
    turns.sort(key=lambda turn: turn.get("ts") or "")
    if not turns:
      continue

    first_request = next((raw.get("_first_request") for raw in owner_turns
                          if raw.get("_first_request")), "")
    last_turn = turns[-1]
    running_turns = [turn for turn in turns if turn["status"] == "running"]
    summary_turn = running_turns[-1] if running_turns else last_turn
    changed = any(turn["outcome"].startswith(("Updated ", "Built ")) for turn in turns)
    title = (stored_title if not _title_is_raw(stored_title)
             else _title_from_request(first_request, last_turn["area"], changed))
    status, result = _chat_status(turns)
    ts = max((turn["ts"] for turn in turns if turn.get("ts")), default=None)
    doc = {
      "schema": SCHEMA_VERSION, "chat_id": chat_id, "provider": provider,
      "title": clip_line(title, 120), "outcome": summary_turn["outcome"],
      "prompt_full": clip_markdown(first_request, FULL_PROMPT_CAP) or "",
      "ts": ts,
      "waiting_for_input": bool(
        attribution.chats.get(chat_id, {}).get("waiting_for_input")),
      "input_kind": attribution.chats.get(chat_id, {}).get("input_kind"),
      "turns": turns,
      "timeline": _build_timeline(
        chat_id, provider, merged, retained_platform_events,
        runs_by_chat.get(chat_id, []), turns,
        agents_omitted=agents_omitted, events_omitted=events_omitted),
    }
    chats[chat_id] = doc
    for helper in merged.values():
      public_id = helper["agent_id"]
      if not helper.get("brief_full"):
        continue
      helpers_out[public_id] = {
        "schema": SCHEMA_VERSION, "agent_id": public_id, "chat_id": chat_id,
        "brief_full": helper["brief_full"],
      }

  chats, helpers_out, chats_omitted = _retain_recent_chats(chats, helpers_out)
  index = _build_index(chats, helpers_out, now, chats_omitted=chats_omitted)
  return index, chats, helpers_out, unlinked


def _cap_report(text: str) -> str:
  out = scrub(text)
  if len(out.encode("utf-8")) <= FINAL_REPORT_CAP:
    return out
  # Cap on bytes (UTF-8) so a report of multibyte chars can't exceed the limit.
  return out.encode("utf-8")[:FINAL_REPORT_CAP].decode("utf-8", "ignore") + "…"


def _retain_recent_chats(chats: dict[str, dict], helpers: dict[str, dict]
                         ) -> tuple[dict[str, dict], dict[str, dict], int]:
  """Keep the newest bounded journal core beneath its reserved byte budget."""
  ordered = sorted(chats.items(), key=lambda item: (
    _iso_to_epoch(item[1].get("ts")) or float("-inf"), item[0]), reverse=True)
  kept: dict[str, dict] = {}
  total = 0
  for chat_id, doc in ordered[:MAX_JOURNAL_CHATS]:
    size = len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    if kept and total + size > BASE_ARTIFACT_TARGET_BYTES:
      continue
    kept[chat_id] = doc
    total += size
  keep_ids = set(kept)
  retained_helpers = {
    helper_id: doc for helper_id, doc in helpers.items()
    if doc.get("chat_id") in keep_ids
  }
  return kept, retained_helpers, len(chats) - len(kept)


def _document_status(doc: dict) -> tuple[str, str]:
  """Small, evidence-backed workflow state for the observation journal.

  Owner attention is never inferred from failed checks or cautious prose. A
  workflow is waiting only when the platform exposes a live question/secure
  input receipt; failure requires a failed root run or a helper failure that
  ended without a usable delivery.
  """
  if doc.get("waiting_for_input"):
    return "waiting", "waiting for you"
  status, result = _chat_status(doc.get("turns") or [])
  runs = ((doc.get("timeline") or {}).get("main_runs") or [])
  if not runs:
    if status == "running":
      return "running", "active"
    if result == "couldn't complete":
      return "failed", "failed"
    if result == "stopped":
      return "stopped", "stopped"
    return "done", "completed"
  latest = max(runs, key=lambda run: (
    _iso_to_epoch(run.get("started_at")) or float("-inf"), str(run.get("id") or "")))
  run_status = str(latest.get("status") or "").lower()
  if run_status in ("running", "resume_pending"):
    return "running", "active"
  if run_status == "failed":
    return "failed", "failed"
  if run_status in ("stopped", "interrupted", "cancelled", "canceled"):
    return "stopped", "stopped"
  if run_status in ("parked", "parked_notified"):
    return "stopped", "paused"
  if result == "couldn't complete":
    return "failed", "failed"
  return "done", "completed"


def _build_index(chats: dict[str, dict], helpers_out: dict[str, dict], now: float,
                 chats_omitted: int = 0) -> dict:
  entries: list[dict] = []
  for chat_id, doc in chats.items():
    status, result = _document_status(doc)
    row = {
      "chat_id": chat_id, "provider": doc["provider"], "title": doc["title"],
      "outcome": doc["outcome"], "result": result,
      "status": status,
      "waiting_for_input": bool(doc.get("waiting_for_input")),
      "input_kind": doc.get("input_kind"),
      "tasks": len((doc.get("timeline") or {}).get("agents") or []),
      "ts": doc.get("ts"),
    }
    entries.append(row)
  entries.sort(key=lambda row: row.get("ts") or "", reverse=True)
  return {"schema": SCHEMA_VERSION, "updated_at": _epoch_to_iso(now),
          "entries": entries,
          "history": {"chats_omitted": max(0, int(chats_omitted))}}


# --- storage sink -----------------------------------------------------------

class StorageSink(Protocol):
  def put(self, rel_path: str, doc: dict) -> None: ...
  def delete(self, rel_path: str) -> None: ...


class HttpSink:
  """Writes documents through the platform storage API with the app token.

  `.json` bodies are the raw document (the storage route stores a `.json` PUT
  verbatim). Every write and delete is recorded on `self.writes` for the
  cron-log summary and the caller's return value — observability the caller
  reads, not hidden mutation.
  """

  def __init__(self, base_url: str, app_id: str, token: str):
    self.base = f"{base_url.rstrip('/')}/api/storage/apps/{app_id}"
    self.token = token
    self.writes: list[str] = []

  def put(self, rel_path: str, doc: dict) -> None:
    body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    self._request("PUT", rel_path, body)
    self.writes.append(f"PUT {rel_path} ({len(body)}b)")

  def delete(self, rel_path: str) -> None:
    self._request("DELETE", rel_path, None)
    self.writes.append(f"DELETE {rel_path}")

  def _request(self, method: str, rel_path: str, body: Optional[bytes]) -> None:
    req = urllib.request.Request(
      f"{self.base}/{rel_path}", data=body, method=method,
      headers={"Authorization": f"Bearer {self.token}",
               "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=API_WRITE_TIMEOUT_SECS):
      pass


class DictSink:
  """In-memory sink for --selftest: collects documents so asserts can inspect
  exactly what would have been written, no network."""

  def __init__(self):
    self.docs: dict[str, dict] = {}
    self.writes: list[str] = []

  def put(self, rel_path: str, doc: dict) -> None:
    self.docs[rel_path] = doc
    self.writes.append(f"PUT {rel_path}")

  def delete(self, rel_path: str) -> None:
    self.docs.pop(rel_path, None)
    self.writes.append(f"DELETE {rel_path}")


class LocalSink:
  """Local-only dry-run sink. It never calls the app storage API.

  Paths are resolved under `root` and rejected if they would escape it. Deletes
  apply only to files generated beneath that local root, so a dry run cannot
  mutate production storage or its database.
  """

  def __init__(self, root: Path):
    self.root = root.resolve()
    self.root.mkdir(parents=True, exist_ok=True)
    self.writes: list[str] = []

  def _path(self, rel_path: str) -> Path:
    path = (self.root / rel_path).resolve()
    if self.root != path and self.root not in path.parents:
      raise ValueError(f"output path escapes dry-run directory: {rel_path}")
    return path

  def put(self, rel_path: str, doc: dict) -> None:
    path = self._path(rel_path)
    save_json(path, doc)
    self.writes.append(f"PUT {rel_path}")

  def delete(self, rel_path: str) -> None:
    path = self._path(rel_path)
    try:
      path.unlink()
    except FileNotFoundError:
      pass
    self.writes.append(f"DELETE {rel_path}")


def flush_documents(index: dict, chats: dict[str, dict],
                    helpers: dict[str, dict], sink: StorageSink,
                    digests: dict[str, str],
                    cap_bytes: Optional[int] = None) -> list[str]:
  """Writes only changed documents (content-hash gate) and evicts helper pages
  over the self-cap. Returns the ordered list of storage paths actually written
  so the caller can log/return exactly what happened."""
  helper_ids = enforce_app_cap(index, chats, helpers, cap_bytes)
  # Chat summaries remain under the cap, but a branch must not advertise a
  # prompt document that was evicted. Preserve the history and disable only the
  # disclosure affordance for omitted helper documents. Re-evaluate because
  # the literal `false` is one byte larger than `true`; the cap applies to the
  # exact shape that will be published.
  while True:
    retained = set(helper_ids)
    for chat in chats.values():
      for turn in chat.get("turns", []):
        for sub in turn.get("subs", []):
          if sub.get("agent_id") not in retained:
            sub["prompt_available"] = False
    revised = enforce_app_cap(index, chats, helpers, cap_bytes)
    if revised == helper_ids:
      break
    helper_ids = revised
  written: list[str] = []

  def _put(path: str, doc: dict) -> None:
    digest = hashlib.sha256(
      json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    if digests.get(path) == digest:
      return
    sink.put(path, doc)
    digests[path] = digest
    written.append(path)

  # Publish dependencies before their parents. A failed write can leave an old
  # index pointing at old-but-valid documents, never a new index pointing at a
  # chat/helper page that was not written yet.
  for agent_id in helper_ids:
    _put(f"helpers/{agent_id}.json", helpers[agent_id])
  for chat_id, doc in chats.items():
    _put(f"chats/{chat_id}.json", doc)
  _put("index.json", index)

  # A page that dropped below the cap this run (or lost its chat) is deleted so
  # the app never serves a stale detail page the roster no longer references.
  live_paths = {f"helpers/{agent_id}.json" for agent_id in helper_ids}
  live_paths.add("index.json")
  live_paths.update(f"chats/{c}.json" for c in chats)
  for stale in [p for p in digests if p not in live_paths]:
    sink.delete(stale)
    digests.pop(stale, None)
    written.append(f"(deleted) {stale}")
  return written


def enforce_app_cap(index: dict, chats: dict[str, dict],
                    helpers: dict[str, dict],
                    cap_bytes: Optional[int] = None) -> list[str]:
  """Returns the helper ids to keep under `APP_ARTIFACT_CAP_BYTES`.

  Roster (index) + chat summaries are never evicted — only helper prompt documents,
  oldest-chat-first (LRU by the chat's last activity), so the app keeps its
  navigable shape and only loses the deepest, oldest detail."""
  limit = APP_ARTIFACT_CAP_BYTES if cap_bytes is None else max(0, cap_bytes)
  activity: dict[str, str] = {r["chat_id"]: r.get("ts") or ""
                              for r in index["entries"]}
  keys = sorted(helpers.keys(),
                key=lambda key: activity.get(helpers[key].get("chat_id"), ""),
                reverse=True)
  base = len(json.dumps(index).encode()) + sum(
    len(json.dumps(d).encode()) for d in chats.values())
  kept: list[str] = []
  total = base
  for key in keys:
    size = len(json.dumps(helpers[key], ensure_ascii=False).encode("utf-8"))
    if total + size > limit:
      continue
    kept.append(key)
    total += size
  return kept


# --- small time/fs helpers --------------------------------------------------

def _sorted_by_mtime(paths: Iterable[Path]) -> list[Path]:
  def _key(p: Path) -> float:
    try:
      return p.stat().st_mtime
    except OSError:
      return 0.0
  return sorted(paths, key=_key, reverse=True)


def _mtime_iso(path: Path) -> Optional[str]:
  try:
    return _epoch_to_iso(path.stat().st_mtime)
  except OSError:
    return None


def _epoch_to_iso(epoch: float) -> str:
  return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def _iso_to_epoch(ts_iso: Optional[str]) -> Optional[float]:
  if not ts_iso or not isinstance(ts_iso, str):
    return None
  s = ts_iso.strip().replace("Z", "+00:00")
  # Trim fractional seconds to 6 digits so datetime can parse them, then let a
  # cheap struct-time path handle the common no-fraction/no-offset shapes.
  try:
    from datetime import datetime
    return datetime.fromisoformat(s).timestamp()
  except ValueError:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
      try:
        return time.mktime(time.strptime(s[:19], fmt))
      except ValueError:
        continue
  return None


# --- top-level refresh ------------------------------------------------------

def run_refresh(cc_dir: Path, codex_home: Path, state_dir: Path,
                base_url: str, app_id: str, app_token: str,
                service_token: str,
                sink: Optional[StorageSink] = None) -> dict:
  """One scan-budgeted refresh slice: parse deltas, attribute, write changed docs.

  Returns a summary dict (also the source of the one-line cron log). When the
  owner-API inputs can't be fetched the slice is a NO-OP that preserves the
  last-good documents — see the degraded branch below."""
  budget = Budget(BUDGET_SECS, BUDGET_BYTES)
  now = time.time()
  model = load_json(state_dir / "model.json", None) or _new_model()
  previous_schema = model.get("schema")
  reset_owner_state = previous_schema not in (2, 3, SCHEMA_VERSION)
  # Trace accumulators are forward-compatible. v2 owner turns are migrated in
  # place below; only pre-v2 rail/gist caches require a bounded owner-chat rescan.
  model["schema"] = SCHEMA_VERSION
  cursors = CursorStore(state_dir / "cursors.json")
  digests = load_json(state_dir / "digests.json", {})
  scanned = load_json(state_dir / "scanned-chats.json", {})
  if not isinstance(scanned, dict):
    # Migrate the old list-of-ids shape: keep them scanned, but eligible to
    # rescan the moment their activity advances (the "" mark is < any real
    # activity_at) so build_tooluse_map's staleness fix covers them too.
    scanned = {str(c): "" for c in scanned} if isinstance(scanned, list) else {}

  parse_claude(cc_dir, model, cursors, budget)
  parse_codex(codex_home, model, cursors, budget)
  _enforce_parent_invariant(model)
  _mark_expired_sources(model, cc_dir, codex_home)

  # Owner-API inputs. A FETCH FAILURE (missing token, timeout, 5xx, malformed
  # body) is NOT an empty roster: rebuilding storage on top of the good docs
  # from an all-unlinked model would DELETE them. Distinguish failure from empty
  # and ABORT the publish, leaving the last-good documents untouched (finding
  # #1). We still persist the local-trace parse progress — model + cursors move
  # together, so the bytes consumed this slice are never lost — but touch NOTHING
  # owner-derived: no digests, no storage writes/deletes.
  links_ok, links = fetch_session_links(base_url, service_token)
  chats_ok, chats_meta = fetch_chats(base_url, service_token)
  if not (links_ok and chats_ok):
    reason = _degraded_reason(service_token, links_ok, chats_ok)
    # A pre-v2 owner cache still needs its one-time reset on the first healthy
    # run. Do not let a degraded trace-only slice advance the shared model
    # marker and accidentally make that later run treat the old cache as v3.
    if reset_owner_state:
      if previous_schema is None:
        model.pop("schema", None)
      else:
        model["schema"] = previous_schema
    save_json(state_dir / "model.json", model)
    cursors.save()
    return {
      "chats": 0, "unlinked": 0, "agents": 0, "writes": 0,
      "bytes_parsed": budget.bytes_read, "budget_exhausted": budget.exhausted,
      "written_paths": [], "degraded": True, "degraded_reason": reason,
    }

  lifecycle_path = state_dir / "agent-lifecycle-v4.json"
  lifecycle_state = load_json(lifecycle_path, {})
  if not isinstance(lifecycle_state, dict):
    lifecycle_state = {}
  if lifecycle_state.get("schema") != LIFECYCLE_CACHE_SCHEMA:
    # Event support is part of the cursor contract. Replaying from zero on a
    # parser-schema bump recovers event types older parsers intentionally
    # skipped for privacy/forward compatibility.
    lifecycle_state = {}
  try:
    lifecycle_after_id = max(0, int(lifecycle_state.get("after_id") or 0))
  except (TypeError, ValueError):
    lifecycle_after_id = 0
  try:
    lifecycle_runs_after_id = max(0, int(lifecycle_state.get("runs_after_id") or 0))
  except (TypeError, ValueError):
    lifecycle_runs_after_id = 0
  had_roster_snapshot = isinstance(lifecycle_state.get("visible_chat_ids"), list)
  previous_visible = {
    str(chat_id) for chat_id in lifecycle_state.get("visible_chat_ids", [])
    if isinstance(chat_id, str)
  }
  current_visible = set(chats_meta)
  (lifecycle_ok, lifecycle_supported, lifecycle_events, lifecycle_runs,
   lifecycle_cursor, lifecycle_runs_cursor) = (
    fetch_agent_lifecycle(
      base_url, service_token, lifecycle_after_id, lifecycle_runs_after_id))
  lifecycle_stale = not lifecycle_ok
  if lifecycle_ok and lifecycle_supported:
    lifecycle_state = merge_lifecycle_state(
      lifecycle_state, lifecycle_events, lifecycle_runs, lifecycle_cursor,
      runs_cursor=lifecycle_runs_cursor, preferred_chat_ids=current_visible)
    # The global cursor is immutable, while chat visibility is not. A recovered
    # chat may have old event ids below that cursor, so newly visible roster
    # members get one scoped snapshot replay. Initial install is already a full
    # global replay and avoids N per-chat requests.
    known_visible = previous_visible & current_visible
    known_lifecycle = {
      str(chat_id) for chat_id in lifecycle_state.get("known_lifecycle_chat_ids", [])
      if isinstance(chat_id, str)
    }
    cached_lifecycle_chats = {
      str(row.get("chat_id"))
      for row in lifecycle_state.get("events", [])
      if isinstance(row, dict) and row.get("chat_id")
    }
    chats_needing_runs = {
      str(row.get("chat_id"))
      for row in lifecycle_state.get("events", [])
      if isinstance(row, dict) and row.get("chat_id") and row.get("chat_run_id")
    }
    cached_run_chats = {
      str(row.get("chat_id"))
      for row in lifecycle_state.get("runs", [])
      if isinstance(row, dict) and row.get("chat_id")
    }
    replay_ids = set(current_visible - previous_visible) if had_roster_snapshot else set()
    # A busy newer chat can evict an older chat from the bounded global cache
    # without changing roster visibility. Remember which chats ever had facts
    # and replay any such missing current chat from its scoped snapshot.
    replay_ids.update((known_lifecycle & current_visible) - cached_lifecycle_chats)
    replay_ids.update((chats_needing_runs & current_visible) - cached_run_chats)
    for replay_chat_id in sorted(replay_ids):
      (replay_ok, replay_supported, replay_events, replay_runs,
       _, _) = fetch_agent_lifecycle(
         base_url, service_token, 0, 0, chat_id=replay_chat_id)
      if not (replay_ok and replay_supported):
        lifecycle_stale = True
        continue
      lifecycle_state = merge_lifecycle_state(
        lifecycle_state, replay_events, replay_runs,
        lifecycle_state.get("after_id", lifecycle_cursor),
        runs_cursor=lifecycle_state.get("runs_after_id", lifecycle_runs_cursor),
        preferred_chat_ids=current_visible, pinned_chat_ids={replay_chat_id},
        count_new_events=False)
      known_visible.add(replay_chat_id)
    if not had_roster_snapshot:
      known_visible = current_visible
    lifecycle_state["visible_chat_ids"] = sorted(known_visible)
  # A missing endpoint or transient failure never clears a prior successful
  # prefix. With no prior prefix, trace evidence remains the honest fallback.
  cached_lifecycle_events = (lifecycle_state.get("events", [])
                             if isinstance(lifecycle_state.get("events"), list) else [])
  cached_lifecycle_runs = (lifecycle_state.get("runs", [])
                           if isinstance(lifecycle_state.get("runs"), list) else [])
  cached_event_counts: dict[str, int] = {}
  for event in cached_lifecycle_events:
    if isinstance(event, dict) and event.get("chat_id"):
      chat_id = str(event["chat_id"])
      cached_event_counts[chat_id] = cached_event_counts.get(chat_id, 0) + 1
  raw_seen_counts = lifecycle_state.get("events_seen_by_chat")
  lifecycle_events_omitted = {
    chat_id: max(0, int(count or 0) - cached_event_counts.get(chat_id, 0))
    for chat_id, count in (raw_seen_counts.items()
                           if isinstance(raw_seen_counts, dict) else [])
    if chat_id in current_visible
  }

  tooluse_map = load_json(state_dir / "tooluse-map.json", {})
  if any(tuid not in tooluse_map
         for s in model["sessions"].values() for tuid in s.get("tool_use_ids", [])):
    tooluse_map.update(build_tooluse_map(
      base_url, service_token, chats_meta, scanned, budget))

  # Helpers the chats record about themselves. This runs every slice (not only
  # when the trace side wants attribution) because it is the only route that
  # covers a chat whose helper traces have already aged off disk.
  # Schema v3 uses a fresh progressive cursor. Besides avoiding any assumptions
  # about the old cache shape, this one-time backfill re-reads historical owner
  # prompts with FULL_PROMPT_CAP instead of preserving v2's report-sized cap.
  helper_scanned_path = state_dir / "scanned-chat-helpers-v3.json"
  helper_scanned = load_json(helper_scanned_path, {})
  chat_helpers_state = _clean_chat_helpers_state(
    load_json(state_dir / "chat-helpers.json", {}))
  chat_turns_state = load_json(state_dir / "chat-turns.json", {})
  chat_inputs_path = state_dir / "chat-inputs.json"
  chat_inputs_state = load_json(chat_inputs_path, {})
  if not isinstance(chat_inputs_state, dict):
    chat_inputs_state = {}
  if reset_owner_state:
    helper_scanned = {}
    chat_helpers_state = {}
    chat_turns_state = {}
    chat_inputs_state = {}
  else:
    # Idempotent shape migration: safe even if a previous degraded run updated
    # model.json before owner API access returned and the turn cache was saved.
    chat_turns_state = _compact_chat_turns_state(chat_turns_state)
  new_helpers, new_turns, new_inputs, rescanned = scan_chat_helpers(
    base_url, service_token, chats_meta, helper_scanned, budget)
  chat_helpers_state.update(new_helpers)
  chat_inputs_state.update(new_inputs)
  # A rescanned chat replaces its turns wholesale (the transcript is append-only,
  # so the fresh walk is authoritative); chats not rescanned this slice keep the
  # turns already on disk.
  chat_turns_state.update(new_turns)
  # A chat rescanned to an EMPTY result (its spawns were compacted away) must be
  # cleared, not left as stale state an overlay-only update can never remove.
  for cid in rescanned:
    if cid not in new_helpers:
      chat_helpers_state.pop(cid, None)
    if cid not in new_turns:
      chat_turns_state.pop(cid, None)
    if cid not in new_inputs:
      chat_inputs_state.pop(cid, None)

  # The chat roster is authoritative for question cards; the bounded transcript
  # scan contributes only safe secure-input receipt state. Neither pauses,
  # failures nor cautious prose are allowed to invent owner work.
  for cid, meta in chats_meta.items():
    if meta.get("waiting_for_input"):
      meta["input_kind"] = "question"
    elif chat_inputs_state.get(cid) == "secure_input":
      meta["waiting_for_input"] = True
      meta["input_kind"] = "secure_input"

  attribution = Attribution(links, tooluse_map, chats_meta)
  index, chats, helpers, unlinked = build_documents(
    model, attribution, now, chat_helpers=chat_helpers_state,
    chat_turns=chat_turns_state,
    lifecycle_events=cached_lifecycle_events,
    lifecycle_runs=cached_lifecycle_runs,
    lifecycle_events_omitted=lifecycle_events_omitted)

  output_sink: StorageSink = sink or HttpSink(base_url, app_id, app_token)
  written = flush_documents(index, chats, helpers, output_sink, digests)

  save_json(state_dir / "model.json", model)
  cursors.save()
  save_json(state_dir / "digests.json", digests)
  save_json(state_dir / "scanned-chats.json", scanned)
  save_json(state_dir / "tooluse-map.json", tooluse_map)
  save_json(helper_scanned_path, helper_scanned)
  save_json(state_dir / "chat-helpers.json", chat_helpers_state)
  save_json(state_dir / "chat-turns.json", chat_turns_state)
  save_json(chat_inputs_path, chat_inputs_state)
  if lifecycle_state:
    save_json(lifecycle_path, lifecycle_state)

  return {
    "chats": len(index["entries"]),
    "unlinked": len(unlinked),
    "agents": len(helpers),
    "writes": len(written),
    "bytes_parsed": budget.bytes_read,
    "budget_exhausted": budget.exhausted,
    "written_paths": written,
    "degraded": False, "lifecycle_stale": lifecycle_stale,
    "lifecycle_supported": lifecycle_supported,
  }


def _degraded_reason(token: str, links_ok: bool, chats_ok: bool) -> str:
  """A one-line reason for a skipped-degraded slice (cron log + stderr)."""
  if not token:
    return ("no service token on disk; cannot read owner chats — "
            "skipped to preserve last-good documents")
  failed = [name for name, ok in (("chats-roster", chats_ok),
                                  ("session-links", links_ok)) if not ok]
  return ("owner-API fetch failed (" + ", ".join(failed) +
          "); skipped to preserve last-good documents")


def _mark_expired_sources(model: dict, cc_dir: Path, codex_home: Path) -> None:
  """Marks a helper `source_expired` when its digest exists but the raw trace
  file is gone (log rotation / cleanup). We keep the digest — the app can still
  show what it knew — but the status derivation degrades to `unavailable`."""
  for agent in model["agents"].values():
    sid = agent["sid"]
    if agent["run_kind"] == "collab":
      # Codex helper trace is the rollout itself; presence-check is cheap enough
      # to skip here (rollouts are dated dirs), so leave collab as-is.
      continue
    agent["source_expired"] = not _claude_trace_exists(cc_dir, sid, agent)


def _claude_trace_exists(cc_dir: Path, sid: str, agent: dict) -> bool:
  base = cc_dir / "projects" / "-data" / sid / "subagents"
  agent_id = agent["agent_id"]
  if agent["run_kind"] == "tasks":
    return (base / f"agent-{agent_id}.jsonl").exists()
  return (base / "workflows" / agent["run_id"] / f"agent-{agent_id}.jsonl").exists()


# --- self-test --------------------------------------------------------------

def _write(path: Path, text: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(text, encoding="utf-8")


def _build_fixture(root: Path) -> tuple[Path, Path]:
  """Fabricates a Claude + Codex trace tree exercising every run kind."""
  fake_secret = "sk-" + "ant-" + "SECRETSECRETSECRET123"
  cc = root / "claude"
  sid = "11111111-1111-1111-1111-111111111111"
  proj = cc / "projects" / "-data"
  # main session file (mtime -> last_activity) + a task subagent with a
  # toolUseId that a chat's Task block will match.
  _write(proj / f"{sid}.jsonl", json.dumps({"type": "user", "timestamp": "2026-07-17T10:00:00Z"}) + "\n")
  sub = proj / sid / "subagents"
  _write(sub / "agent-taskA.meta.json",
         json.dumps({"agentType": "general-purpose", "description": "Investigate X",
                     "toolUseId": "toolu_TASKA", "spawnDepth": 1}))
  _write(sub / "agent-taskA.jsonl", "\n".join([
    json.dumps({"type": "user", "timestamp": "2026-07-17T10:00:01Z",
                "message": {"role": "user", "content": "Investigate the flaky test"}}),
    json.dumps({"type": "assistant", "timestamp": "2026-07-17T10:00:02Z",
                "message": {"role": "assistant",
                            "content": [
                              {"type": "tool_use", "name": "Read",
                               "input": {"file_path": "/data/x.py"}},
                              {"type": "tool_use", "name": "Task", "id": "toolu_NESTED",
                               "input": {"description": "Check nested fixture"}}],
                            "usage": {"input_tokens": 100, "output_tokens": 20}}}),
    json.dumps({"type": "assistant", "timestamp": "2026-07-17T10:00:03Z",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text":
                                         f"Root cause found: token {fake_secret} in log."}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 5, "output_tokens": 40}}}),
  ]) + "\n")
  _write(sub / "agent-taskNested.meta.json",
         json.dumps({"agentType": "general-purpose", "description": "Check nested fixture",
                     "toolUseId": "toolu_NESTED", "spawnDepth": 2}))
  _write(sub / "agent-taskNested.jsonl", "\n".join([
    json.dumps({"type": "user", "timestamp": "2026-07-17T10:00:02Z",
                "message": {"role": "user", "content": "Check the nested fixture"}}),
    json.dumps({"type": "assistant", "timestamp": "2026-07-17T10:00:02.500Z",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "Nested check complete."}],
                            "stop_reason": "end_turn"}}),
  ]) + "\n")
  # a workflow run: record + phases + one journal-completed agent.
  wf_id = "wf_abc123"
  _write(proj / sid / "workflows" / f"{wf_id}.json", json.dumps({
    "runId": wf_id, "timestamp": "2026-07-17T09:00:00Z",
    "script": "export const meta = {\n  name: 'verify-merge',\n  description: 'Adversarially verify the merge',\n  phases: [{ title: 'Verify' }, { title: 'Report' }],\n}\n",
  }))
  wdir = proj / sid / "subagents" / "workflows" / wf_id
  _write(wdir / "agent-wfB.meta.json", json.dumps({"agentType": "general-purpose", "spawnDepth": 1}))
  _write(wdir / "agent-wfB.jsonl", "\n".join([
    json.dumps({"type": "user", "timestamp": "2026-07-17T09:00:01Z",
                "message": {"role": "user", "content": "Verify the merge is coherent"}}),
    json.dumps({"type": "assistant", "timestamp": "2026-07-17T09:00:05Z",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "Verified: clean."}],
                            "usage": {"input_tokens": 50, "output_tokens": 10}}}),
  ]) + "\n")
  _write(wdir / "journal.jsonl",
         json.dumps({"type": "started", "agentId": "wfB", "key": "v2:k"}) + "\n" +
         # A helper the runtime launched whose result never landed (turn died
         # mid-fleet). It leaves no transcript; only the journal knows it ran.
         json.dumps({"type": "started", "agentId": "wfLost", "key": "v2:l"}) + "\n" +
         json.dumps({"type": "result", "agentId": "wfB", "key": "v2:k",
                     "result": {"verdict": "clean", "summary": "Merge is coherent; no leftover markers."}}) + "\n")
  # a task board card labelling the tasks run.
  _write(cc / "tasks" / sid / "1.json",
         json.dumps({"id": "1", "subject": "Interview flaky test", "status": "completed"}))

  # Codex: a parent rollout with a synthetic collab item + a child rollout
  # whose parent_thread_id links back to the parent.
  codex = root / "codex"
  day = codex / "sessions" / "2026" / "07" / "17"
  parent_sid = "019f0000-parent"
  child_sid = "019f0000-child0"
  _write(day / f"rollout-2026-07-17T12-00-00-{parent_sid}.jsonl", "\n".join([
    json.dumps({"timestamp": "2026-07-17T12:00:00Z", "type": "session_meta",
                "payload": {"id": parent_sid, "session_id": parent_sid, "cwd": "/data"}}),
    json.dumps({"timestamp": "2026-07-17T12:00:01Z", "type": "response_item",
                "payload": {"type": "collabAgentToolCall", "senderThreadId": parent_sid,
                            "receiverThreadIds": [child_sid],
                            "agentsStates": {child_sid: {"model": "gpt-5-codex",
                                                          "status": "completed",
                                                          "prompt": "Refactor the parser"}}}}),
  ]) + "\n")
  _write(day / f"rollout-2026-07-17T12-05-00-{child_sid}.jsonl", "\n".join([
    json.dumps({"timestamp": "2026-07-17T12:05:00Z", "type": "session_meta",
                "payload": {"id": child_sid, "session_id": child_sid, "cwd": "/data",
                            "parent_thread_id": parent_sid}}),
    json.dumps({"timestamp": "2026-07-17T12:05:01Z", "type": "response_item",
                "payload": {"type": "function_call", "name": "shell",
                            "arguments": "{\"command\": \"pytest\"}"}}),
    json.dumps({"timestamp": "2026-07-17T12:05:02Z", "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer",
                            "message": "Refactor complete, tests pass."}}),
    json.dumps({"timestamp": "2026-07-17T12:05:03Z", "type": "event_msg",
                "payload": {"type": "task_complete",
                            "completed_at": "2026-07-17T12:05:03Z",
                            "duration_ms": 3000,
                            "last_agent_message": "Refactor complete, tests pass."}}),
  ]) + "\n")
  return cc, codex


def _assert(cond: bool, msg: str) -> None:
  if not cond:
    raise AssertionError(msg)


def selftest() -> int:
  import tempfile
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    cc, codex = _build_fixture(root)
    state = root / "state"
    state.mkdir()
    budget = Budget(BUDGET_SECS, BUDGET_BYTES)
    now = time.time()
    model = _new_model()
    cursors = CursorStore(state / "cursors.json")
    parse_claude(cc, model, cursors, budget)
    parse_codex(codex, model, cursors, budget)
    _enforce_parent_invariant(model)
    _mark_expired_sources(model, cc, codex)

    # Attribution fixtures: session-links covers the Claude session; the Codex
    # parent is linked; the child inherits via parent_thread_id. Links are keyed
    # by (provider, session_id) — the backend's true identity (finding #2).
    claude_sid = "11111111-1111-1111-1111-111111111111"
    links = {("claude", claude_sid): "chatA", ("codex", "019f0000-parent"): "chatC"}
    chats_meta = {"chatA": {"title": "Fix flaky test", "provider": "claude"},
                  "chatC": {"title": "Refactor parser", "provider": "codex"}}
    attribution = Attribution(links, {"toolu_TASKA": "chatA"}, chats_meta)
    fixture_request = "Please fix the flaky test and verify the result."
    index, chats, helpers, unlinked = build_documents(
      model, attribution, now,
      chat_turns={"chatA": [{
        "_agent_ids": ["taskA"], "_tools": [], "_original": "Investigation started.",
        "_first_request": fixture_request, "ts": "2026-07-17T10:00:00Z", "nblocks": 1,
      }]})

    sink = DictSink()
    written = flush_documents(index, chats, helpers, sink, {})
    _assert(written and written[-1] == "index.json"
            and all(path.startswith("helpers/") for path in written[:len(helpers)]),
            f"leaf documents publish before index: {written[:3]} … {written[-1:]}")

    # --- schema-shape asserts (json-schema-ish) ---
    _assert(index["schema"] == SCHEMA_VERSION, "index schema")
    _assert(isinstance(index["updated_at"], str), "index.updated_at is ISO")
    _assert(set(index) == {"schema", "updated_at", "entries", "history"},
            f"index keys: {set(index)}")
    _assert(index["history"] == {"chats_omitted": 0}, "journal retention is explicit")
    _assert(len(index["entries"]) == 2,
            f"expected 2 chats, got {len(index['entries'])}")
    for row in index["entries"]:
      _assert(set(row) == {"chat_id", "provider", "title", "outcome",
                           "result", "status", "waiting_for_input", "input_kind",
                           "tasks", "ts"},
              f"index entry keys: {set(row)}")

    doc_a = chats["chatA"]
    _assert(doc_a["provider"] == "claude", "chatA provider")
    _assert(set(doc_a) == {"schema", "chat_id", "provider", "title", "outcome",
                           "prompt_full", "ts", "waiting_for_input", "input_kind",
                           "turns", "timeline"},
            f"chat keys: {set(doc_a)}")
    _assert(set(doc_a["timeline"]) == {
      "main_agent_id", "main_runs", "agents", "events", "retention"},
            f"timeline keys: {set(doc_a['timeline'])}")
    _assert(doc_a["timeline"]["retention"] == {
      "agents_omitted": 0, "events_omitted": 0},
      "timeline retention is explicit")
    _assert(doc_a["prompt_full"] == fixture_request, "root prompt is published")
    _assert(doc_a["turns"], "chat has derived turns")
    for turn in doc_a["turns"]:
      _assert(set(turn) == {"outcome", "area", "result", "status", "flag",
                            "note", "ts", "subs"},
              f"turn keys: {set(turn)}")
      for sub in turn["subs"]:
        _assert(set(sub) == {"agent_id", "kind", "name", "ask", "state",
                             "depth", "prompt_available"},
                f"sub keys: {set(sub)}")
    # The fleet journal launched wfB + wfLost but only wfB reported: the turn
    # carries an honest display-only completeness note, and stays un-flagged.
    wf_turn = next((turn for turn in doc_a["turns"]
                    if any(sub["agent_id"] == "wfB" for sub in turn["subs"])), None)
    _assert(wf_turn is not None, "workflow fleet turn present")
    _assert(wf_turn["note"] == "1 of 2 helpers never reported a result, "
            "so this outcome may reflect partial work.",
            f"lost-helper note derived from journal counts: {wf_turn.get('note')}")
    _assert(all(turn["note"] is None for turn in doc_a["turns"]
                if turn is not wf_turn),
            "fully-reported turns carry no completeness note")

    doc_c = chats["chatC"]
    _assert(doc_c["provider"] == "codex", "chatC provider")
    _assert(any(sub["state"] == "done" for turn in doc_c["turns"]
                for sub in turn["subs"]), "collab helper done")

    # --- product-truth asserts ---
    page = sink.docs["helpers/wfB.json"]
    _assert(set(page) == {"schema", "agent_id", "chat_id", "brief_full"},
            f"helper prompt keys: {set(page)}")
    # secret scrubbing reached the task agent's final report/outcome.
    task_page = sink.docs["helpers/taskA.json"]
    secret_prefix = "sk-" + "ant-SECRET"
    _assert(secret_prefix not in json.dumps(task_page), "secret scrubbed from helper page")
    _assert(secret_prefix not in json.dumps(index), "secret scrubbed from index")
    task_agent = model["agents"][f"{claude_sid}::taskA"]
    _assert(task_agent["final_report_terminal"] is True,
            "Claude end_turn marks a terminal report")
    task_agent["source_expired"] = True
    _mark_expired_sources(model, cc, codex)
    _assert(task_agent["source_expired"] is False,
            "a returned source clears the expired marker")

    # child inherited the parent's chat via parent_thread_id.
    child_agent_ids = {sub["agent_id"] for turn in doc_c["turns"] for sub in turn["subs"]}
    _assert("019f0000-child0" in child_agent_ids, "codex child linked to parent chat")
    _assert(model["agents"]["019f0000-child0::019f0000-child0"]["final_report_terminal"] is True,
            "Codex task_complete marks a terminal report")
    codex_child = model["agents"]["019f0000-child0::019f0000-child0"]
    _assert(codex_child["started_at"] == "2026-07-17T12:05:00+00:00"
            and codex_child["ended_at"] == "2026-07-17T12:05:03Z"
            and codex_child["started_time_quality"] == "exact"
            and codex_child["ended_time_quality"] == "exact",
            f"Codex completed_at-duration gives exact bounds: {codex_child}")
    nested = model["agents"][f"{claude_sid}::taskNested"]
    _assert(nested["parent_agent_id"] == "taskA" and nested["spawn_depth"] == 2,
            f"Claude nested parent resolves by tool id: {nested}")
    nested_public = next(agent for agent in doc_a["timeline"]["agents"]
                         if agent["agent_id"] == "taskNested")
    _assert(nested_public["parent_agent_id"] == "taskA"
            and nested_public["start_time_quality"] == "observed",
            f"nested timeline preserves exact edge and observed bound: {nested_public}")

    # Platform lifecycle is authoritative over trace status/timing while the
    # trace remains the source for the lazily-loaded, scrubbed prompt.
    platform_rows = [
      {"id": 12, "event_key": "a-terminal", "chat_id": "chatA",
       "chat_run_id": "run-A", "provider": "claude",
       "provider_session_id": claude_sid, "agent_id": "opaque-a",
       "provider_agent_id": "taskA", "parent_agent_id": None,
       "type": "agent_terminal", "state": "done", "agent_type": "general-purpose",
       "summary": "Platform completion", "occurred_at": "2026-07-17T10:00:04Z",
       "observed_at": "2026-07-17T10:00:04Z", "time_quality": "exact",
       "source": "runner", "source_event_id": "native-terminal"},
      # Intentionally listed after the terminal and timestamped later; causal
      # ordering must still put start before terminal without altering raw time.
      {"id": 11, "event_key": "a-start", "chat_id": "chatA",
       "chat_run_id": "run-A", "provider": "claude",
       "provider_session_id": claude_sid, "agent_id": "opaque-a",
       "provider_agent_id": "taskA", "parent_agent_id": None,
       "type": "agent_started", "state": "running", "agent_type": "general-purpose",
       "summary": "Investigate the flaky test", "occurred_at": "2026-07-17T10:00:05Z",
       "observed_at": "2026-07-17T10:00:05Z", "time_quality": "exact",
       "source": "runner", "source_event_id": "native-start"},
      {"id": 13, "event_key": "b-start", "chat_id": "chatA",
       "chat_run_id": "run-A", "provider": "claude",
       "provider_session_id": claude_sid, "agent_id": "opaque-b",
       "provider_agent_id": "provider-b", "parent_agent_id": None,
       "type": "agent_started", "state": "running", "agent_type": "research",
       "summary": "Review overlapping work", "occurred_at": None,
       "observed_at": "2026-07-17T10:00:02Z", "time_quality": "observed",
       "source": "runner", "source_event_id": "native-b-start"},
    ]
    platform_events = [_normalized_lifecycle_event(row) for row in platform_rows]
    _assert(all(platform_events), "platform fixture normalizes")
    model_platform = json.loads(json.dumps(model))
    model_platform["agents"][f"{claude_sid}::taskA"]["result"] = "failed"
    _, platform_chats, platform_helpers, _ = build_documents(
      model_platform, attribution, now,
      chat_turns={"chatA": [{
        "_agent_ids": ["taskA"], "_tools": [], "_original": "Investigation started.",
        "_first_request": fixture_request, "ts": "2026-07-17T10:00:00Z",
      }]}, lifecycle_events=platform_events,
      lifecycle_runs=[{"id": "run-A", "chat_id": "chatA", "provider": "claude",
                       "status": "completed", "started_at": "2026-07-17T10:00:00Z",
                       "ended_at": "2026-07-17T10:00:06Z"}])
    platform_doc = platform_chats["chatA"]
    opaque_a = next(agent for agent in platform_doc["timeline"]["agents"]
                    if agent["agent_id"] == "opaque-a")
    opaque_b = next(agent for agent in platform_doc["timeline"]["agents"]
                    if agent["agent_id"] == "opaque-b")
    _assert(opaque_a["state"] == "done", "platform terminal wins over failed trace")
    _assert(opaque_a["started_at"] is None and opaque_a["timing_conflict"] is True,
            f"contradictory aggregate bounds are suppressed: {opaque_a}")
    _assert(any(sub["agent_id"] == "opaque-a" for turn in platform_doc["turns"]
                for sub in turn["subs"]),
            "native turn retains its platform-renamed helper")
    _assert(platform_helpers["opaque-a"]["brief_full"] == "Investigate the flaky test",
            "platform overlay retains the trace prompt document")
    _assert(opaque_b["parent_agent_id"] is None and opaque_b["ended_at"] is None
            and opaque_b["start_time_quality"] == "observed"
            and opaque_b["started_at"] == "2026-07-17T10:00:02Z",
            f"observed-only open helper stays honest: {opaque_b}")
    a_events = [event for event in platform_doc["timeline"]["events"]
                if event["subject_agent_id"] == "opaque-a"]
    _assert([event["type"] for event in a_events] == ["agent_started", "agent_terminal"],
            f"terminal-before-start input is causally ordered: {a_events}")
    checkpoints = [event for event in platform_doc["timeline"]["events"]
                   if event["type"] == "main_checkpoint"]
    _assert(checkpoints and all(event["subject_agent_id"] == "main"
                                for event in checkpoints),
            f"owner turns become canonical main checkpoints: {checkpoints}")
    _assert(checkpoints[0]["order"]
            < next(event["order"] for event in platform_doc["timeline"]["events"]
                   if event["event_id"] == "platform-" + hashlib.sha256(
                     b"b-start").hexdigest()[:24]),
            "owner and helper events share one timestamp-ordered stream")
    failed_index = _build_index({"chatA": {
      **platform_doc,
      "timeline": {**platform_doc["timeline"], "main_runs": [
        *platform_doc["timeline"]["main_runs"],
        {"id": "run-latest-failed", "status": "failed",
         "started_at": "2026-07-17T10:10:00Z", "ended_at": "2026-07-17T10:10:01Z"},
      ]},
    }}, {}, now)
    _assert(failed_index["entries"][0]["status"] == "failed"
            and failed_index["entries"][0]["result"] == "failed",
            "latest durable root failure remains visible without an attention inbox")

    same_run_failure_index = _build_index({"chatA": {
      **platform_doc,
      "timeline": {
        **platform_doc["timeline"],
        "agents": [
          {**agent, "chat_run_id": "run-current-failed", "state": "failed"}
          if agent["agent_id"] == "opaque-a" else agent
          for agent in platform_doc["timeline"]["agents"]
        ],
        "main_runs": [
          *platform_doc["timeline"]["main_runs"],
          {"id": "run-current-failed", "status": "failed",
           "started_at": "2026-07-17T10:10:00Z",
           "ended_at": "2026-07-17T10:10:01Z"},
        ],
      },
    }}, {}, now)
    _assert(same_run_failure_index["entries"][0]["status"] == "failed",
            "a failed root run stays failed when its helper also failed")

    delivered_after_failure = _build_v3_turn({
      "_agent_ids": ["recoverable"], "_tools": [],
      "_facts": {"area_evidence": [], "verb": "Updated",
                 "changed": True, "verification": "none"},
      "_original": "Updated the chat UI and verified the final interaction.",
      "_first_request": "",
    }, {"recoverable": {
      "agent_id": "recoverable", "kind": "codex", "name": "Codex",
      "ask": "Investigate one failed path", "state": "failed", "depth": 1,
      "brief_full": "Investigate one failed path", "_tools": [],
    }}, "the chat UI")
    _assert(delivered_after_failure["status"] == "done",
            f"a recovered helper failure stays visible without alarming the owner: {delivered_after_failure}")

    running_index = _build_index({"chatA": {
      **platform_doc,
      "timeline": {**platform_doc["timeline"], "main_runs": [
        *platform_doc["timeline"]["main_runs"],
        {"id": "run-latest-running", "status": "running",
         "started_at": "2026-07-17T10:20:00Z", "ended_at": None},
      ]},
    }}, {}, now)
    _assert(running_index["entries"][0]["status"] == "running",
            "active work is visible without an owner-attention concept")

    waiting_index = _build_index({"chatA": {
      **platform_doc,
      "waiting_for_input": True,
      "input_kind": "question",
    }}, {}, now)
    waiting_entry = waiting_index["entries"][0]
    _assert(waiting_entry["status"] == "waiting"
            and waiting_entry["result"] == "waiting for you"
            and waiting_entry["input_kind"] == "question",
            f"only a concrete open input becomes waiting: {waiting_entry}")

    secure_messages = [{"role": "assistant", "blocks": [{
      "type": "secure_input", "request_id": "secure-1", "status": "pending",
      "title": "Connect service", "fields": [{"name": "key", "label": "API key"}],
    }]}]
    _assert(_pending_secure_input(secure_messages),
            "a pending secure-input receipt is concrete owner input")
    secure_messages[0]["blocks"][0]["status"] = "completed"
    _assert(not _pending_secure_input(secure_messages),
            "a settled secure-input receipt no longer waits for the owner")

    duplicate_timeline = _build_timeline(
      "chat-duplicates", "codex", {
        "stopped-a": {
          "agent_id": "stopped-a", "parent_agent_id": "main",
          "ask": "Review the parser", "brief_full": "Review the parser carefully.",
          "lifecycle_state": "stopped", "result": "",
          "started_at": "2026-07-17T10:29:00Z",
          "last_activity_at": "2026-07-17T10:30:00Z",
        },
        "unknown-b": {
          "agent_id": "unknown-b", "parent_agent_id": "main",
          "ask": "Review the parser", "brief_full": "Review the parser carefully.",
          "lifecycle_state": "unknown", "result": "",
          "started_at": "2026-07-17T10:30:01Z",
          "last_activity_at": "2026-07-17T10:31:00Z",
        },
        "failed-retry": {
          "agent_id": "failed-retry", "parent_agent_id": "main",
          "ask": "Review the parser", "brief_full": "Review the parser carefully.",
          "lifecycle_state": "failed", "result": "The retry failed.",
          "started_at": "2026-07-17T10:31:01Z",
          "last_activity_at": "2026-07-17T10:32:00Z",
        },
      }, [], [], [])
    duplicate_agents = duplicate_timeline["agents"]
    _assert(len(duplicate_agents) == 1
            and duplicate_agents[0]["agent_id"] == "failed-retry"
            and duplicate_agents[0]["state"] == "failed"
            and duplicate_agents[0]["attempt_count"] == 3
            and duplicate_agents[0]["attempt_states"]
            == {"stopped": 1, "unknown": 1, "failed": 1}
            and duplicate_timeline["retention"]["agents_omitted"] == 2,
            f"resolved duplicate work is summarized without hiding an unresolved retry: {duplicate_agents}")

    parallel_same_prompt = _build_timeline(
      "chat-parallel-same-prompt", "codex", {
        "parallel-a": {
          "agent_id": "parallel-a", "parent_agent_id": "main",
          "ask": "Review independently", "brief_full": "Review independently.",
          "lifecycle_state": "unknown",
          "started_at": "2026-07-17T11:00:00Z",
          "last_activity_at": "2026-07-17T11:05:00Z",
        },
        "parallel-b": {
          "agent_id": "parallel-b", "parent_agent_id": "main",
          "ask": "Review independently", "brief_full": "Review independently.",
          "lifecycle_state": "done",
          "started_at": "2026-07-17T11:00:01Z",
          "last_activity_at": "2026-07-17T11:04:00Z",
        },
      }, [], [], [])
    _assert(len(parallel_same_prompt["agents"]) == 2,
            "overlapping same-prompt helpers remain distinct ensemble members")

    repeated_completed = _build_timeline(
      "chat-repeated-completed", "codex", {
        "completed-a": {
          "agent_id": "completed-a", "parent_agent_id": "main",
          "ask": "Review independently", "brief_full": "Review independently.",
          "lifecycle_state": "done",
          "started_at": "2026-07-17T12:00:00Z",
          "last_activity_at": "2026-07-17T12:05:00Z",
        },
        "completed-b": {
          "agent_id": "completed-b", "parent_agent_id": "main",
          "ask": "Review independently", "brief_full": "Review independently.",
          "lifecycle_state": "done",
          "started_at": "2026-07-17T12:10:00Z",
          "last_activity_at": "2026-07-17T12:15:00Z",
        },
      }, [], [], [])
    _assert(len(repeated_completed["agents"]) == 2,
            "same work repeated after completion remains two intentional runs")

    lossy_shared_summary = _build_timeline(
      "chat-shared-summary", "codex", {
        "area-a": {
          "agent_id": "area-a", "parent_agent_id": "main",
          "ask": "Investigate the shared symptom",
          "brief_full": "SYMPTOM: Missing reply.\nYOUR AREA: Trace persistence.",
          "lifecycle_state": "stopped",
          "started_at": "2026-07-17T13:00:00Z",
          "last_activity_at": "2026-07-17T13:05:00Z",
        },
        "area-b": {
          "agent_id": "area-b", "parent_agent_id": "main",
          "ask": "Investigate the shared symptom",
          "brief_full": "SYMPTOM: Missing reply.\nYOUR AREA: Trace rendering.",
          "lifecycle_state": "done",
          "started_at": "2026-07-17T13:10:00Z",
          "last_activity_at": "2026-07-17T13:15:00Z",
        },
      }, [], [], [])
    _assert(len(lossy_shared_summary["agents"]) == 2,
            "lossy shared summaries never collapse distinct full assignments")

    # A lifecycle-only chat must render without any trace/chat-block evidence.
    life_only_raw = {"id": 20, "event_key": "only-start", "chat_id": "chatOnly",
                     "chat_run_id": "run-only", "provider": "codex",
                     "agent_id": "only-agent", "provider_agent_id": "native-only",
                     "parent_agent_id": None, "type": "agent_started", "state": "running",
                     "agent_type": "codex", "summary": "Inspect lifecycle-only data",
                     "occurred_at": "2026-07-17T11:00:00Z",
                     "observed_at": "2026-07-17T11:00:00Z", "time_quality": "exact",
                     "source": "runner", "source_event_id": "only-native"}
    life_only = _normalized_lifecycle_event(life_only_raw)
    _assert(_normalized_lifecycle_event(
      dict(life_only_raw, agent_id="../escape")) is None,
      "lifecycle identities cannot escape helper storage paths")
    life_attribution = Attribution({}, {}, {
      "chatOnly": {"title": "Lifecycle only", "provider": "codex"}})
    _, life_chats, life_helpers, _ = build_documents(
      _new_model(), life_attribution, now, lifecycle_events=[life_only])
    _assert(life_chats["chatOnly"]["timeline"]["agents"][0]["agent_id"] == "only-agent",
            "lifecycle-only chat renders without trace evidence")
    _assert(not life_helpers
            and life_chats["chatOnly"]["turns"][0]["subs"][0]["prompt_available"] is False,
            "lifecycle-only helpers do not advertise or emit empty prompt documents")
    _, deleted_chats, _, _ = build_documents(
      _new_model(), Attribution({}, {}, {}), now, lifecycle_events=[life_only])
    _assert(not deleted_chats,
            "cached lifecycle rows cannot republish a chat absent from the owner roster")

    # A platform start is additive evidence, not proof that a trace-terminal
    # Codex helper resumed. Current Codex instrumentation has no positive done
    # marker, so retaining this stronger trace terminal is essential.
    codex_start_raw = dict(
      life_only_raw, id=22, event_key="codex-start-only", chat_id="chatC",
      provider_agent_id="019f0000-child0", agent_id="opaque-codex",
      summary="Refactor the parser")
    _, start_only_chats, _, _ = build_documents(
      model, attribution, now,
      lifecycle_events=[_normalized_lifecycle_event(codex_start_raw)])
    start_only_agent = next(
      agent for agent in start_only_chats["chatC"]["timeline"]["agents"]
      if agent["agent_id"] == "opaque-codex")
    _assert(start_only_agent["state"] == "done",
            f"start-only evidence cannot revive trace completion: {start_only_agent}")

    # Re-running ancestry reconciliation must remove a once-unique edge if a
    # later incremental parse reveals a second possible owner.
    ambiguous_model = json.loads(json.dumps(model))
    second_owner = _agent(ambiguous_model, claude_sid, "tasks", "taskOther", "tasks")
    second_owner["spawned_tool_use_ids"] = ["toolu_NESTED"]
    _resolve_claude_parents(ambiguous_model, claude_sid, "tasks")
    _assert(ambiguous_model["agents"][f"{claude_sid}::taskNested"]["parent_agent_id"] is None,
            "ambiguous Claude replay clears a formerly unique parent")

    observed_model = _new_model()
    _fold_codex_subagent_activity({
      "type": "sub_agent_activity", "agent_thread_id": "observed-child",
      "kind": "started"}, "observed-root", observed_model,
      "2026-07-17T12:00:00Z")
    observed_agent = observed_model["agents"]["observed-root::observed-child"]
    _assert(observed_agent["started_time_quality"] == "observed",
            "record timestamp fallback is observed, not provider-exact")

    prior_agent_cap = globals()["MAX_TIMELINE_AGENTS"]
    try:
      globals()["MAX_TIMELINE_AGENTS"] = 2
      retained, omitted = _retain_recent_helpers({
        "old": {"agent_id": "old", "started_at": "2026-01-01T00:00:00Z"},
        "mid": {"agent_id": "mid", "started_at": "2026-02-01T00:00:00Z"},
        "new": {"agent_id": "new", "started_at": "2026-03-01T00:00:00Z"},
      })
      _assert(set(retained) == {"mid", "new"} and omitted == 1,
              "helper retention is bounded, recent and explicit")
    finally:
      globals()["MAX_TIMELINE_AGENTS"] = prior_agent_cap

    prior_cache_cap = globals()["MAX_LIFECYCLE_CACHE_EVENTS"]
    try:
      globals()["MAX_LIFECYCLE_CACHE_EVENTS"] = 2
      saturated = {
        "schema": LIFECYCLE_CACHE_SCHEMA, "after_id": 100,
        "events": [
          {"id": 99, "event_id": "e99", "chat_id": "new-a"},
          {"id": 100, "event_id": "e100", "chat_id": "new-b"},
        ],
        "runs": [], "visible_chat_ids": ["recovered"],
        "known_lifecycle_chat_ids": ["recovered"],
        "events_seen_by_chat": {"recovered": 1},
      }
      recovered = merge_lifecycle_state(
        saturated, [{"id": 1, "event_id": "e1", "chat_id": "recovered"}],
        [], 100, preferred_chat_ids={"recovered"}, count_new_events=False)
      _assert("e1" in {row["event_id"] for row in recovered["events"]},
              "scoped replay survives a saturated newer global cache")
      _assert(recovered["events_seen_by_chat"]["recovered"] == 1,
              "scoped replay does not double-count cache omissions")
      all_current = merge_lifecycle_state(
        saturated, [{"id": 1, "event_id": "e1", "chat_id": "recovered"}],
        [], 100, preferred_chat_ids={"recovered", "new-a", "new-b"},
        pinned_chat_ids={"recovered"}, count_new_events=False)
      _assert("e1" in {row["event_id"] for row in all_current["events"]},
              "scoped replay is pinned even when every cached chat is current")
    finally:
      globals()["MAX_LIFECYCLE_CACHE_EVENTS"] = prior_cache_cap

    tombstoned = merge_lifecycle_state(
      {"runs": [{"id": "rolled-back", "chat_id": "chatA",
                  "status": "running", "started_at": None}],
       "events": [], "after_id": 0, "runs_after_id": 1},
      [], [{"update_id": 2, "id": "rolled-back", "chat_id": "chatA",
            "status": "deleted", "started_at": None, "ended_at": None}],
      0, runs_cursor=2)
    _assert(not tombstoned["runs"] and tombstoned["runs_after_id"] == 2,
            "run tombstone removes a rolled-back root run without rewinding")

    # Duplicates and overlaps remain one event each in chronological/causal order.
    overlap = [
      _timeline_event("a-s", "agent_started", "a", "main", "running",
                      "2026-07-17T00:00:01Z", None, "exact"),
      _timeline_event("b-s", "agent_started", "b", "main", "running",
                      "2026-07-17T00:00:02Z", None, "exact"),
      _timeline_event("b-e", "agent_terminal", "b", "b", "done",
                      "2026-07-17T00:00:03Z", None, "exact"),
      _timeline_event("a-e", "agent_terminal", "a", "a", "done",
                      "2026-07-17T00:00:04Z", None, "exact"),
      _timeline_event("a-s", "agent_started", "a", "main", "running",
                      "2026-07-17T00:00:01Z", None, "exact"),
    ]
    overlap_ordered = _ordered_timeline_events(overlap, {"a": "main", "b": "main"})
    _assert([row["event_id"] for row in overlap_ordered] == ["a-s", "b-s", "b-e", "a-e"],
            f"overlap and duplicate order is stable: {overlap_ordered}")
    dense = [
      _timeline_event(f"dense-{index}", "agent_started", f"dense-{index}",
                      "main", "running", None, None, "unknown",
                      source_order=index)
      for index in range(2_000)
    ]
    dense_ordered = _ordered_timeline_events(
      dense, {f"dense-{index}": "main" for index in range(2_000)})
    _assert(len(dense_ordered) == 2_000
            and dense_ordered[-1]["event_id"] == "dense-1999",
            "dense causal ordering remains complete and deterministic")
    simultaneous_checkpoints = _ordered_timeline_events([
      _timeline_event("cp-1", "main_checkpoint", "main", "main", "done",
                      "2026-07-17T00:00:02Z", None, "exact", "First result"),
      _timeline_event("cp-2", "main_checkpoint", "main", "main", "done",
                      "2026-07-17T00:00:02Z", None, "exact", "Second result"),
    ], {})
    _assert(len(simultaneous_checkpoints) == 2,
            "distinct owner checkpoints at the same timestamp are retained")

    # Lifecycle pagination commits only complete prefixes; malformed pages are
    # a failed fetch and therefore cannot replace the caller's last-good cache.
    original_api_get = globals()["_api_get_json"]
    fetch_calls: list[str] = []
    terminal_only_raw = dict(life_only_raw, id=21, event_key="only-terminal",
                             type="agent_terminal", state="done",
                             occurred_at="2026-07-17T11:00:02Z",
                             observed_at="2026-07-17T11:00:02Z")
    def _fake_lifecycle_pages(base_url: str, path: str, token: str):
      fetch_calls.append(path)
      if "?after_id=0&" in path:
        return 200, {"events": [life_only_raw], "runs": [],
                     "next_after_id": 20, "next_runs_after_id": 0,
                     "has_more": True, "runs_has_more": False}
      return 200, {"events": [terminal_only_raw],
                   "runs": [{"update_id": 1, "id": "run-only", "chat_id": "chatOnly",
                              "provider": "codex", "status": "done",
                              "started_at": "2026-07-17T11:00:00Z",
                              "ended_at": "2026-07-17T11:00:02Z"}],
                   "next_after_id": 21, "next_runs_after_id": 1,
                   "has_more": False, "runs_has_more": False}
    try:
      globals()["_api_get_json"] = _fake_lifecycle_pages
      (fetch_ok, fetch_supported, fetched_events, fetched_runs,
       fetched_cursor, fetched_runs_cursor) = (
        fetch_agent_lifecycle("http://fixture", "token"))
      _assert(fetch_ok and fetch_supported and fetched_cursor == 21
              and fetched_runs_cursor == 1
              and len(fetched_events) == 2 and len(fetched_runs) == 1
              and len(fetch_calls) == 2,
              f"lifecycle pages form one complete prefix: {fetch_calls}")
      cached_once = merge_lifecycle_state(
        {}, fetched_events, fetched_runs, fetched_cursor,
        runs_cursor=fetched_runs_cursor)
      cached_twice = merge_lifecycle_state(
        cached_once, fetched_events, fetched_runs, fetched_cursor,
        runs_cursor=fetched_runs_cursor)
      _assert(len(cached_twice["events"]) == 2 and len(cached_twice["runs"]) == 1,
              "lifecycle cache merge is idempotent")
      globals()["_api_get_json"] = lambda *_args: (200, {
        "events": "truncated", "runs": [], "next_after_id": 22,
        "next_runs_after_id": 1, "has_more": False, "runs_has_more": False})
      bad_ok, _, bad_events, bad_runs, bad_cursor, bad_runs_cursor = fetch_agent_lifecycle(
        "http://fixture", "token", after_id=21)
      _assert(not bad_ok and not bad_events and not bad_runs and bad_cursor == 21
              and bad_runs_cursor == 0,
              "malformed lifecycle page preserves the caller's prior cursor")
    finally:
      globals()["_api_get_json"] = original_api_get

    # Existing schema-3 accumulators gain v4 defaults lazily, and Codex parent
    # thread ids resolve only when the complete ancestry evidence is present.
    legacy = {
      "schema": 3,
      "sessions": {
        "root": {"provider": "codex", "last_activity_at": None,
                 "parent_thread_id": None, "tool_use_ids": []},
        "nested": {"provider": "codex", "last_activity_at": None,
                   "parent_thread_id": "root", "tool_use_ids": []},
        "hollow": {"provider": "codex", "last_activity_at": None,
                   "parent_thread_id": "root", "tool_use_ids": []},
      },
      "agents": {}, "runs": {},
    }
    migrated = _agent(legacy, "nested", "collab", "nested", "collab")
    _session(legacy, "root", "codex")
    migrated["parent_agent_id"] = "root"
    grandchild = _agent(legacy, "nested", "collab", "grandchild", "collab")
    grandchild["parent_agent_id"] = "nested"
    unknown_parent = _agent(legacy, "nested", "collab", "orphan", "collab")
    unknown_parent["parent_agent_id"] = "missing-thread"
    missing_parent_agent = _agent(legacy, "nested", "collab", "partial", "collab")
    missing_parent_agent["parent_agent_id"] = "hollow"
    _enforce_parent_invariant(legacy)
    _assert("spawned_tool_use_ids" in migrated and "spawn_depth" in legacy["sessions"]["root"],
            "schema-3 state receives all v4 defaults")
    _assert(migrated["parent_agent_id"] == "main"
            and grandchild["parent_agent_id"] == "nested"
            and unknown_parent["parent_agent_id"] is None
            and missing_parent_agent["parent_agent_id"] is None,
            "known nested ancestry is retained while unknown ancestry stays unknown")

    # === finding-specific asserts =========================================

    # #6: a long file path survives scrubbing (paths no longer collapse to a
    # redacted token), while structured secrets are still caught.
    long_path = "/data/apps/workflows/state/subagent_activity_digest_model.json"
    _assert("redacted" not in scrub(long_path), f"path survives scrub: {scrub(long_path)!r}")
    _assert("[redacted-token]" in scrub("blob abcdefABCDEF0123456789abcdefABCDEF01 end"),
            "unslashed 36-char token still redacted")
    _assert("[redacted-jwt]" in scrub("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123456"),
            "JWT still redacted after divergence")
    summary = _plain_ask(
      "You are a READ-ONLY auditor inside a live instance.",
      "You are a READ-ONLY auditor. Never modify anything.\n\n"
      "PRIORITY TRACK — Diagnose why app-store updates are failing.\n"
      "1. Find the update mechanism.")
    _assert(summary == "Diagnose why app-store updates are failing.",
            f"policy preamble is removed from task summary: {summary!r}")
    subsystem_summary = _plain_ask(
      "Be concrete: every finding needs file and line evidence.",
      "You are a READ-ONLY code reviewer. Do not edit.\n\n"
      "Be concrete: every finding needs file and line evidence.\n\n"
      "Subsystem: shell frontend. Explore /data/shell/src and compare it to the spec.")
    _assert(subsystem_summary == "Shell frontend.",
            f"explicit subsystem beats reusable review instructions: {subsystem_summary!r}")
    area_summary = _plain_ask(
      "Investigate the missing replies.",
      "SYMPTOM: In some chats a reply is missing.\n"
      "CONFIRMED CASES: several historical runs.\n\n"
      "YOUR AREA: Trace the Codex oversized-output completion path.")
    _assert(area_summary == "Trace the Codex oversized-output completion path.",
            f"trailing YOUR AREA disambiguates shared ensemble context: {area_summary!r}")
    app_summary = _plain_ask(
      "Component shape vs the canonical contract.",
      "You are a READ-ONLY app auditor.\nApp: Reflection (id 56).\n"
      "Review storage and theme behavior.")
    _assert(app_summary == "Audit Reflection",
            f"app-labelled audit prompts name the assigned app: {app_summary!r}")
    finding_summary = _plain_ask("{", (
      "You are an adversarial verifier. The reviewer reported this finding:\n\n"
      '{"title":"Queued messages can disappear during restart",'
      '"severity":"high","evidence":"repro"}'))
    _assert(finding_summary == "Verify Queued messages can disappear during restart",
            f"structured finding title becomes the task summary: {finding_summary!r}")
    proposed_fix_summary = _plain_ask(
      "Review the proposed fix.",
      "You are a READ-ONLY adversarial reviewer.\n\nPROPOSED FIX:\n"
      '{"id":"finalize-backstop","change_description":'
      '"Persist a neutral lost-reply marker when finalization has no text."}')
    _assert(
      proposed_fix_summary
      == "Review Persist a neutral lost-reply marker when finalization has no text.",
      f"structured proposed-fix review names its distinct change: {proposed_fix_summary!r}")
    place_summary = _plain_ask(
      "Adversarially fact-check this Sarajevo dining spot.",
      'Fact-check the claim. Place: {"name":"Aščinica ASDŽ",'
      '"category":"home cooking"}. Be skeptical.')
    _assert(place_summary == "Fact-check Aščinica ASDŽ",
            f"structured place name disambiguates repeated tasks: {place_summary!r}")
    _assert(_task_summary("{\n}\n") == "",
            "punctuation-only prompt lines are never published as summaries")
    _assert(helper_from_agent_block({
      "type": "tool", "tool": "Task", "tool_use_id": "call-placeholder",
      "input": "Working in the background", "output": "", "status": "done",
    }) is None, "empty background placeholder is not published as a helper")
    _assert(helper_from_agent_block({
      "type": "tool", "tool": "Task", "tool_use_id": "call-empty",
      "input": "", "output": "", "status": "done",
    }) is None, "empty task shell is not published as a helper")
    _assert(_clean_chat_helpers_state({"chat": [{
      "agent_id": "call_placeholder", "description": None,
      "_brief_full": "", "_full_outcome": None,
    }]}) == {}, "cached background placeholder is removed during migration")
    long_prompt = "Inspect the complete evidence.\n\n" + ("detail " * 300)
    _assert((clip_markdown(long_prompt, FULL_PROMPT_CAP) or "") == long_prompt.strip(),
            "full helper prompt is retained below the safety cap")
    _assert(len((clip_markdown("🛠" * 100, 64) or "").encode("utf-8")) <= 64,
            "markdown caps are byte-accurate for multibyte prompts")
    long_root = "Please inspect the complete workflow.\n\n" + ("context " * 1800)
    _assert(_owner_request(long_root) == long_root.strip(),
            "root prompt no longer passes through the smaller report cap")

    # Free-text title derivation is scrubbed and ends on a word boundary.
    derived_title = _title_from_request(
      "please update abcdefABCDEF0123456789abcdefABCDEF01 safely", "Notes", True)
    _assert("[redacted-token]" in derived_title and "abcdefABCDEF0123" not in derived_title,
            f"derived title scrubbed: {derived_title!r}")

    # #2: (provider, sid) keying — a codex link on the SAME id must not shadow
    # the claude session's own-provider link.
    coll_links = {("claude", claude_sid): "chatA", ("codex", claude_sid): "chatWRONG"}
    cid, creason = Attribution(coll_links, {}, chats_meta).resolve(claude_sid, model["sessions"])
    _assert(cid == "chatA" and creason == "session-link",
            f"provider-keyed link wins: {cid}/{creason}")

    # #10: a parent cycle surfaces as parent-cycle, not masked as parent-unlinked.
    cyc = {"A": {"provider": "codex", "parent_thread_id": "B"},
           "B": {"provider": "codex", "parent_thread_id": "A"}}
    ccid, creason = Attribution({}, {}, {}).resolve("A", cyc)
    _assert(ccid is None and creason == "parent-cycle", f"parent cycle reason: {creason}")

    # A fresh session_meta clears stale parent state, and a self-parent is always
    # normalized to no parent rather than becoming a fake helper/cycle.
    parent_root = root / "parent-invariant" / "sessions" / "2026" / "07" / "17"
    pinv = _new_model()
    _session(pinv, "fresh-top", "codex")["parent_thread_id"] = "stale-parent"
    _write(parent_root / "rollout-fresh-top.jsonl", json.dumps({
      "timestamp": "2026-07-17T00:00:00Z", "type": "session_meta",
      "payload": {"id": "fresh-top", "session_id": "fresh-top"}}) + "\n")
    _write(parent_root / "rollout-self-parent.jsonl", json.dumps({
      "timestamp": "2026-07-17T00:00:00Z", "type": "session_meta",
      "payload": {"id": "self-parent", "session_id": "self-parent",
                  "parent_thread_id": "self-parent"}}) + "\n")
    parse_codex(root / "parent-invariant", pinv, CursorStore(root / "pinv-cur.json"),
                Budget(BUDGET_SECS, BUDGET_BYTES))
    _assert(pinv["sessions"]["fresh-top"]["parent_thread_id"] is None,
            "fresh top-level metadata clears stale parent")
    _assert(pinv["sessions"]["self-parent"]["parent_thread_id"] is None,
            "session is never its own parent")
    _session(pinv, "legacy-self", "codex")["parent_thread_id"] = "legacy-self"
    _agent(pinv, "legacy-self", "collab", "legacy-self", "collab")
    _enforce_parent_invariant(pinv)
    _assert(pinv["sessions"]["legacy-self"]["parent_thread_id"] is None
            and "legacy-self::legacy-self" not in pinv["agents"],
            "stale self-parent helper removed from accumulator")

    # Launch receipts from both providers are unresolved work, not reports.
    ack = helper_from_agent_block({
      "type": "tool", "tool": "Agent", "status": "done",
      "input": "{'description': 'Check parser', 'subagent_type': 'Explore'}",
      "output": "Async agent launched successfully\nagentId: joined"}, scope="chatJ")
    _assert(ack["status"] == "working" and ack["_full_outcome"] is None,
            f"Claude launch receipt is unresolved: {ack}")
    codex_ack = helper_from_agent_block({
      "type": "tool", "tool": "Agent", "status": "done",
      "input": "description=Review storage, subagent_type=codex",
      "output": ("Codex Task started in the background as task-abc123. "
                 "Check /codex:status task-abc123 for progress.\nagentId: codex-bg")},
      scope="chatJ")
    _assert(codex_ack["status"] == "working" and codex_ack["_full_outcome"] is None,
            f"Codex launch receipt is unresolved: {codex_ack}")
    envelope_text = (
      "Async agent launched successfully. (This tool result is internal metadata — "
      "never quote it.)\n\nThe agent is working in the background. You will be notified "
      "automatically when it completes.\noutput_file: /tmp/helper.output")
    envelope_ack = helper_from_agent_block({
      "type": "tool", "tool": "Agent", "status": "done",
      "input": "{'description': 'Review storage'}",
      "output": envelope_text + "\nagentId: envelope-bg"}, scope="chatJ")
    _assert(envelope_ack["status"] == "working"
            and envelope_ack["_full_outcome"] is None,
            f"production launch envelope is unresolved: {envelope_ack}")
    cached_ack_ev = _block_evidence({
      "agent_id": "cached-bg", "status": "finished", "_full_outcome": envelope_text,
      "description": "Review storage"}, "claude")
    _assert(cached_ack_ev["state"] == "running" and not cached_ack_ev["report_full"],
            f"cached launch envelope is normalized: {cached_ack_ev}")

    trace_now = 1_800_000_000.0
    # Procedural prose is not terminal evidence. This covers both freshly
    # parsed traces and cached v2 block records that predate terminal markers.
    progress = "I have good coverage. Let me verify one final detail."
    _assert(derive_status({"final_report": progress, "has_activity": True}, trace_now)
            == "stopped", "cached progress text is stopped")
    _assert(derive_status({"final_report": progress, "has_activity": True,
                           "final_report_terminal": False}, trace_now) == "stopped",
            "explicit non-terminal text is stopped")
    _assert(derive_status({"final_report": "Review complete.", "has_activity": True,
                           "final_report_terminal": True}, trace_now) == "finished",
            "explicit terminal report is finished")
    _assert(derive_status({"final_report": "Review complete.", "has_activity": True,
                           "final_report_terminal": True, "interrupted": True}, trace_now)
            == "stopped", "later interruption supersedes report text")
    _assert(derive_status({"result": "failed", "last_ts": _epoch_to_iso(trace_now)}, trace_now)
            == "failed", "string failure overrides freshness")
    _assert(derive_status({"result": {"collab_status": "cancelled"},
                           "last_ts": _epoch_to_iso(trace_now)}, trace_now)
            == "stopped", "cancelled collab result overrides freshness")
    _assert(derive_status({"result": {"collab_status": "completed"}}, trace_now)
            == "finished", "completed collab result is terminal success")
    cached_progress_ev = _block_evidence({
      "agent_id": "cached-progress", "status": "finished", "_full_outcome": progress,
      "description": "Review storage"}, "claude")
    _assert(cached_progress_ev["state"] == "stopped",
            "cached progress block is stopped")
    _assert(not _is_fresh(_epoch_to_iso(trace_now + FUTURE_SKEW_SECS + 1), trace_now),
            "far-future timestamps are not fresh")
    interrupted_trace = root / "interrupted-agent.jsonl"
    _write(interrupted_trace, "\n".join([
      json.dumps({"timestamp": _epoch_to_iso(trace_now - 30),
                  "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Now let me inspect the next file."}]}}),
      json.dumps({"timestamp": _epoch_to_iso(trace_now - 20),
                  "message": {"role": "user", "content": "[Request interrupted by user]"}}),
    ]) + "\n")
    interrupted_model = _new_model()
    interrupted_agent = _agent(
      interrupted_model, "interrupt-sid", "tasks", "interrupt-agent", "tasks")
    interrupted_agent["goal"] = "Test interruption handling"
    _fold_agent_transcript(
      interrupted_trace, interrupted_agent, interrupted_model, "interrupt-sid",
      CursorStore(root / "interrupt-cursors.json"), Budget(BUDGET_SECS, BUDGET_BYTES))
    _assert(interrupted_agent["interrupted"] is True
            and derive_status(interrupted_agent, trace_now) == "stopped",
            "Claude interruption marker produces stopped lifecycle")

    # A trace holding only a launch receipt stays running while fresh, then
    # becomes stopped rather than remaining green forever. The receipt itself
    # is never exposed as a report.
    trace_ack = {
      "agent_id": "trace-bg", "description": "Review storage", "goal": "Review storage",
      "steps": [],
      "final_report": ("Codex Task started in the background as task-abc123. "
                       "Check /codex:status task-abc123 for progress."),
      "last_ts": _epoch_to_iso(trace_now - FRESH_SECS - 1),
    }
    stale_ack_ev = _trace_evidence(trace_ack, trace_now, "codex")
    _assert(stale_ack_ev["state"] == "stopped" and not stale_ack_ev["report_full"],
            f"stale trace receipt is stopped without report: {stale_ack_ev}")
    trace_ack["last_ts"] = _epoch_to_iso(trace_now - 60)
    fresh_ack_ev = _trace_evidence(trace_ack, trace_now, "codex")
    _assert(fresh_ack_ev["state"] == "running" and not fresh_ack_ev["report_full"],
            f"fresh trace receipt is running without report: {fresh_ack_ev}")

    # Terminal downstream evidence supersedes a superficial async launch ack.
    block_ev = _block_evidence(ack, "claude")
    trace_ev = dict(block_ev, state="done", origin="trace",
                    report_full="Done. Parser check completed.")
    joined = _merge_evidence([block_ev, trace_ev], "claude")
    _assert(joined["state"] == "done", f"terminal evidence wins: {joined['state']}")
    joined_turn = _build_v3_turn({
      "_agent_ids": ["joined"], "_tools": [], "_original": "Done.",
      "ts": 1_700_000_000_000, "nblocks": 1}, {"joined": joined})
    _assert(joined_turn["subs"][0]["state"] == "done",
            "turn and helper use the joined state")

    # If the storage cap evicts a helper page, the retained chat history must
    # no longer present a broken detail-page affordance for that helper.
    cap_index = {"entries": [{"chat_id": "cap", "ts": "2026-07-17T00:00:00Z"}]}
    cap_chats = {"cap": {"turns": [{"subs": [
      {"agent_id": "kept", "prompt_available": True},
      {"agent_id": "evicted", "prompt_available": True},
    ]}]}}
    cap_helpers = {
      "kept": {"chat_id": "cap", "payload": "k" * 100},
      "evicted": {"chat_id": "cap", "payload": "e" * 100},
    }
    cap_base = (len(json.dumps(cap_index).encode())
                + len(json.dumps(cap_chats["cap"]).encode()))
    cap_one = len(json.dumps(cap_helpers["kept"], ensure_ascii=False).encode("utf-8"))
    cap_sink = DictSink()
    flush_documents(cap_index, cap_chats, cap_helpers, cap_sink, {}, cap_base + cap_one + 1)
    cap_subs = cap_sink.docs["chats/cap.json"]["turns"][0]["subs"]
    _assert(cap_subs[0]["prompt_available"] is True
            and cap_subs[1]["prompt_available"] is False,
            f"evicted prompt is not advertised: {cap_subs}")
    _assert("helpers/kept.json" in cap_sink.docs
            and "helpers/evicted.json" not in cap_sink.docs,
            "cap stores only the retained helper page")

    # Production-shaped Beat Machine validator regression: the recorded check
    # says valid=false, so a later "should work" claim cannot become green.
    beat_messages = [
      {"role": "user", "content": "Please update Beat Machine", "ts": 1775497394976},
      {"role": "assistant", "blocks": [
        {"type": "tool", "tool": "Agent", "status": "done", "input":
         "{'subagent_type': 'Explore', 'description': 'Find app build process'}", "output": ""},
        {"type": "tool", "tool": "Bash", "status": "done", "input":
         "curl -X PATCH $API_BASE_URL/api/apps/10", "output": "10"},
        {"type": "tool", "tool": "Bash", "status": "done", "input":
         "curl $API_BASE_URL/api/apps/10/validate", "output":
         '{"valid": false, "issues": ["Compiled JS has no default export — the component won\'t mount."]}'},
        {"type": "text", "content":
         "The validator checks for export default OR export{. The app should work and has been updated."},
      ]},
    ]
    beat_helpers, beat_raw_turns = _walk_chat(beat_messages, scope="beat-chat")
    beat_merged = {h["agent_id"]: _merge_evidence([_block_evidence(h, "claude")], "claude")
                   for h in beat_helpers}
    beat_turn = _build_v3_turn(beat_raw_turns[0], beat_merged)
    _assert(beat_turn["status"] == "attention" and beat_turn["result"] == "not confirmed",
            f"Beat validator truth gate: {beat_turn}")
    _assert(beat_turn["outcome"] == "Investigated Beat Machine",
            f"Beat validator neutral outcome: {beat_turn['outcome']}")
    legacy_beat = {
      "ts": beat_raw_turns[0]["ts"], "_agent_ids": beat_raw_turns[0]["_agent_ids"],
      "_tools": [{
        "tool": "Bash", "status": "done",
        "input": "curl $API_BASE_URL/api/apps/10/validate",
        "output": '{"valid": false, "issues": ["no default export"]}',
      }],
      "_original": beat_raw_turns[0]["_original"],
      "_first_request": "Please update Beat Machine",
    }
    migrated_beat = _compact_chat_turn(legacy_beat, keep_request=True)
    migrated_turn = _build_v3_turn(migrated_beat, beat_merged)
    _assert("_tools" not in migrated_beat and migrated_beat.get("_facts"),
            f"v2 turn migrates without raw tools: {migrated_beat.keys()}")
    _assert((migrated_turn["status"], migrated_turn["result"], migrated_turn["area"])
            == (beat_turn["status"], beat_turn["result"], beat_turn["area"]),
            f"compaction preserves turn truth: {migrated_turn} vs {beat_turn}")

    # Missing verification is the normal case: a plain reported edit stays
    # done, while positive doubt in the agent's own words remains amber.
    ordinary_raw = {
      "_agent_ids": [], "_tools": [{
        "tool": "Edit", "input": "/data/apps/beat-machine/src/App.jsx",
        "output": "", "status": "done"}],
      "_original": "Updated Beat Machine with the requested controls.",
      "ts": 1_700_000_000_000, "nblocks": 1,
    }
    ordinary_turn = _build_v3_turn(ordinary_raw, {})
    _assert(ordinary_turn["status"] == "done" and ordinary_turn["result"] == "done",
            f"untested ordinary edit stays done: {ordinary_turn}")
    hedged_raw = dict(
      ordinary_raw,
      _original="Updated Beat Machine. It should load fine.")
    hedged_turn = _build_v3_turn(hedged_raw, {})
    _assert(hedged_turn["status"] == "attention"
            and hedged_turn["result"] == "not confirmed",
            f"agent hedge stays attention: {hedged_turn}")
    _assert(not _hedges_result(
      "The attempted resolver errored, so I replaced it with a deterministic path."),
      "historical attempt is not a hedge on the delivered result")
    _assert(not _hedges_result(
      "MutationObserver catches changes that ResizeObserver might miss."),
      "old failure-mode explanation is not a hedge on the delivered result")
    _assert(_request_noun(
      "building Mustafina Garaža — a 2030-aesthetic garage site in Bosnian")
      == "Mustafina Garaža", "named new-site request supplies area fallback")
    _assert(_request_noun("build a period tracking app") is None,
            "generic build request does not invent a product name")
    _assert(_delivery_sentence("Here's what's making headlines right now:")
            == "Here's what's making headlines right now",
            "selected delivery lead drops trailing colon")

    # #1: a fetch FAILURE (here: no service token) is distinguished from empty
    # success, so fetch_* report ok=False and run_refresh aborts without writing.
    lok, _ = fetch_session_links("http://127.0.0.1:1", "")
    cok, _ = fetch_chats("http://127.0.0.1:1", "")
    _assert(lok is False and cok is False, "missing token reported as fetch failure")
    deg_state = root / "deg-state"; deg_state.mkdir()
    deg = run_refresh(cc, codex, deg_state, "http://127.0.0.1:1", "appX", "apptok", "")
    _assert(deg.get("degraded") is True, "no-token run is degraded")
    _assert(deg["writes"] == 0, "degraded run writes nothing")
    _assert(not (deg_state / "digests.json").exists(),
            "degraded run leaves owner-derived state (digests) untouched")
    old_deg_state = root / "old-deg-state"; old_deg_state.mkdir()
    old_model = _new_model(); old_model["schema"] = 1
    save_json(old_deg_state / "model.json", old_model)
    save_json(old_deg_state / "chat-turns.json", {"legacy": [{"rail": []}]})
    run_refresh(cc, codex, old_deg_state, "http://127.0.0.1:1", "appX", "apptok", "")
    _assert(load_json(old_deg_state / "model.json", {}).get("schema") == 1,
            "degraded pre-v2 run preserves the reset marker for its next retry")

    # #4: charge only consumed bytes; a partial tail is left for the next run.
    pf = root / "partial.jsonl"
    rec1 = json.dumps({"uuid": "r1"})
    _write(pf, rec1 + "\n" + '{"uuid": "partial-no-newline')
    pb = Budget(BUDGET_SECS, BUDGET_BYTES)
    _pr, precs, pcur = read_delta(pf, {}, pb)
    consumed = len((rec1 + "\n").encode())
    _assert([r.get("uuid") for r in precs] == ["r1"], "only the complete record is read")
    _assert(pb.bytes_read == consumed, f"budget charges only consumed bytes: {pb.bytes_read}")
    _assert(pcur["offset"] == consumed, "cursor left at the partial tail start")

    # #4: an oversized single record is flagged + skipped; later records survive.
    of = root / "oversized.jsonl"
    big_line = json.dumps({"blob": "z" * (MAX_RECORD_BYTES + 64)})
    _write(of, big_line + "\n" + json.dumps({"uuid": "after"}) + "\n")
    ob = Budget(BUDGET_SECS, BUDGET_BYTES)
    _or, orecs, _oc = read_delta(of, {}, ob)
    ouuids = [r.get("uuid") for r in orecs]
    _assert("after" in ouuids and not any(r.get("blob") for r in orecs),
            f"oversized record skipped, later record kept: {ouuids}")

    # #5a: a first-record fingerprint mismatch forces a rescan even when ino +
    # size are unchanged (inode reuse / truncate-and-regrow above the offset).
    ff = root / "fp.jsonl"
    _write(ff, json.dumps({"uuid": "u1"}) + "\n")
    fb = Budget(BUDGET_SECS, BUDGET_BYTES)
    _fr, _frecs, fcur = read_delta(ff, {}, fb)
    _assert(_fr is False and fcur.get("first_fp"), "initial read records a fingerprint")
    tampered = dict(fcur); tampered["first_fp"] = "0000000000000000"
    fr2, frecs2, _fc2 = read_delta(ff, tampered, Budget(BUDGET_SECS, BUDGET_BYTES))
    _assert(fr2 is True and [r.get("uuid") for r in frecs2] == ["u1"],
            "fingerprint mismatch triggers a full rescan")

    # #5b: a replaced (shrunk) Codex rollout resets its derived steps — no dupe.
    cx = root / "codex5b"
    day5 = cx / "sessions" / "2026" / "07" / "17"
    psid5, csid5 = "p5-thread", "c5-thread"
    def _child_rollout(n_steps: int) -> str:
      lines = [json.dumps({"timestamp": "2026-07-17T12:05:00Z", "type": "session_meta",
                           "payload": {"id": csid5, "session_id": csid5,
                                       "parent_thread_id": psid5}})]
      for i in range(n_steps):
        lines.append(json.dumps({"timestamp": "2026-07-17T12:05:%02dZ" % (i + 1),
                                 "type": "response_item",
                                 "payload": {"type": "function_call", "name": "shell",
                                             "arguments": "{}"}}))
      return "\n".join(lines) + "\n"
    rf5 = day5 / f"rollout-2026-07-17T12-05-00-{csid5}.jsonl"
    _write(rf5, _child_rollout(3))
    m5 = _new_model(); cur5 = CursorStore(root / "cx5-cur.json")
    parse_codex(cx, m5, cur5, Budget(BUDGET_SECS, BUDGET_BYTES))
    _write(rf5, _child_rollout(2))   # shrink -> size < offset -> rescan
    parse_codex(cx, m5, cur5, Budget(BUDGET_SECS, BUDGET_BYTES))
    n5 = len(m5["agents"][f"{csid5}::{csid5}"]["steps"])
    _assert(n5 == 2, f"replaced codex rollout resets steps (no dupe): got {n5}")

    # #8: a since-cleared board failure no longer sticks.
    bstate = root / "board8"; sid8 = "sid8"
    m8 = _new_model(); _run(m8, sid8, "tasks", "tasks", "t"); _agent(m8, sid8, "tasks", "a8", "tasks")
    bdir = bstate / sid8
    _write(bdir / "1.json", json.dumps({"subject": "do", "status": "failed"}))
    _parse_task_board(bdir, sid8, m8)
    _assert(m8["agents"][f"{sid8}::a8"]["board_status"] == "failed", "board failure set")
    _write(bdir / "1.json", json.dumps({"subject": "do", "status": "completed"}))
    _parse_task_board(bdir, sid8, m8)
    _assert(m8["agents"][f"{sid8}::a8"]["board_status"] is None,
            "board failure cleared once no card is failing")

    print("SELFTEST OK")
    print(f"  chats={len(index['entries'])} unlinked={len(unlinked)} "
          f"helpers={len(helpers)} writes={len(written)}")
    kinds = {sub["kind"] for doc in chats.values() for turn in doc["turns"]
             for sub in turn["subs"]}
    print(f"  helper kinds={sorted(kinds)}")
    print(f"  task prompt chars={len(task_page['brief_full'])}")
    return 0


# --- entrypoint -------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
  parser = argparse.ArgumentParser(description="Digest agent-run traces for the Workflows app.")
  parser.add_argument("--selftest", action="store_true",
                      help="run the offline fixture self-test and exit")
  parser.add_argument("--app-id", default=os.environ.get("APP_ID", ""))
  parser.add_argument("--dry-run", action="store_true",
                      help="write documents locally; never call app storage")
  parser.add_argument("--out", type=Path,
                      help="local output directory (required with --dry-run)")
  args = parser.parse_args(argv)

  if args.selftest:
    return selftest()

  data_dir = Path(os.environ.get("DATA_DIR", "/data"))
  base_url = os.environ.get("API_BASE_URL", "http://localhost:8000")
  app_token = os.environ.get("APP_TOKEN", "")
  app_id = args.app_id or os.environ.get("APP_ID", "")
  cc_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(data_dir / "cli-auth" / "claude")))
  codex_home = Path(os.environ.get("CODEX_HOME", str(data_dir / "cli-auth" / "codex")))
  if args.dry_run and args.out is None:
    parser.error("--dry-run requires --out <local-directory>")
  if args.out is not None and not args.dry_run:
    parser.error("--out is only valid with --dry-run")
  state_dir = ((args.out / ".state") if args.dry_run else
               Path(os.environ.get("WORKFLOWS_STATE_DIR",
                                   str(data_dir / "apps" / "workflows" / "state"))))
  # Owner reads use the service token (owner JWT); app routes reject it and the
  # app token, symmetrically. The service token is read from disk (the job env
  # deliberately excludes it), following the Reflection precedent.
  service_token = ""
  token_file = data_dir / "service-token.txt"
  try:
    service_token = token_file.read_text(encoding="utf-8").strip()
  except OSError:
    pass

  if not args.dry_run and (not app_token or not app_id):
    print("workflows-refresh: missing APP_TOKEN/APP_ID; cannot write storage", file=sys.stderr)
    return 2

  state_dir.mkdir(parents=True, exist_ok=True)
  output_sink = LocalSink(args.out) if args.dry_run else None
  summary = run_refresh(cc_dir, codex_home, state_dir, base_url, app_id,
                        app_token, service_token, sink=output_sink)
  if summary.get("degraded"):
    # Owner-API inputs were unavailable: nothing was published or deleted, the
    # last-good documents are intact. Exit 4 (distinct from the shell wrapper's
    # 0/2/3/5) so a caller can tell a preservation skip from a real success or a
    # parser crash.
    print(f"workflows-refresh: SKIPPED (degraded): {summary['degraded_reason']}",
          file=sys.stderr)
    print(f"workflows-refresh: skipped-degraded ({summary['degraded_reason']}) "
          f"parsed={summary['bytes_parsed']}b writes=0")
    return 4
  mode = "dry-run " if args.dry_run else ""
  print(f"workflows-refresh: {mode}chats={summary['chats']} unlinked={summary['unlinked']} "
        f"agents={summary['agents']} writes={summary['writes']} "
        f"parsed={summary['bytes_parsed']}b exhausted={summary['budget_exhausted']}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())


# Smells
# - _result_is_success returns a slightly awkward double-negative for collab
#   statuses; kept for now because the collab vocabulary (inProgress/completed/
#   failed) is fixture-only until a live collab sample exists — revisit with
#   real data rather than guess more branches now.
# - The main Claude session <sid>.jsonl body is intentionally NOT streamed (only
#   its mtime is read): chat-level tokens_total aggregates helper tokens, not
#   top-level turn tokens, to stay within budget. Documented; revisit only if a
#   consumer needs true per-chat token totals.
# - build_tooluse_map is a bounded fallback that yields nothing on instances
#   whose subagents are all workflow/collab (no Task blocks in chat messages);
#   that is correct-but-untested against live Task data here — the selftest wires
#   it via fixture instead.
