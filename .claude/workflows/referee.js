export const meta = {
  name: 'referee',
  description: 'Autonomous REFEREE review of PDFs: harness advances, isolated Sonnet subagents answer and seal each task',
  whenToUse: 'Review one or more paper PDFs end to end with no human steps',
  phases: [{ title: 'Review', detail: 'per paper: tasks -> isolated workers -> seal, until done' }],
}

// args: { papers: ["papers/x.pdf" | "<paper-id>", ...], repo: "C:/.../single-harness",
//         python: "../.venv/Scripts/python", env: "PYTHONUTF8=1 SH_PROJECTS_DIR=... ...",
//         maxRounds: 20 }
// Model work is done ONLY by isolated Sonnet workers (one context per task) and a Haiku
// controller that runs the harness command. Worker model is clamped to haiku|sonnet: never
// Opus. Token budget: workers use the minimal `referee-worker` agent type (Read/Write/Bash
// only) when it is registered, and a two-tool-call protocol (read prompt; write+seal in one
// Bash call). Whether a task sealed is read back from the harness's own task list, never
// from the worker's say-so.
const A = args || {}
const REPO = A.repo
const PY = A.python || '../.venv/Scripts/python'
const ENV = A.env || 'PYTHONUTF8=1'
// Rounds, not tasks: lenses -> grades -> certificates/reconstructions -> proof maps and check
// plans -> their step certificates -> up to SH_MAX_REVISIONS revision rounds each. The harness
// holds a case open while verification is owed, so a round cap is the only stop besides it.
const MAX_ROUNDS = A.maxRounds || 20
const sh = cmd => `cd "${REPO}" && ${ENV} ${PY} ${cmd}`

const STATE = {
  type: 'object',
  properties: {
    paper_id: { type: 'string' }, phase: { type: 'string' }, status: { type: 'string' },
    blocked_reason: { type: 'string' },
    // `status` is the workflow's; these say what the review actually CHECKED.
    scientific_status: { type: 'string' }, scientific_blocker: { type: 'string' },
    tasks: { type: 'array', items: { type: 'object', properties: {
      id: { type: 'string' }, role: { type: 'string' }, prompt: { type: 'string' },
      out: { type: 'string' }, model: { type: 'string' }, effort: { type: 'string' },
      after: { type: 'array', items: { type: 'string' } } },
      required: ['id', 'role', 'prompt', 'out', 'model', 'effort', 'after'] } },
  },
  required: ['paper_id', 'phase', 'status', 'tasks'],
}

// Minimal agent types (.claude/agents/referee-*.md) load at session start; a session that
// predates them falls back to the default workflow agent with the same prompt.
const minimal = { worker: true, controller: true }
async function run(kind, prompt, opts) {
  if (minimal[kind]) {
    try {
      return await agent(prompt, { ...opts, agentType: `referee-${kind}` })
    } catch (e) {
      if (!String(e).includes('not found')) throw e
      minimal[kind] = false
      log(`referee-${kind} agent type not registered in this session; using the default agent`)
    }
  }
  return agent(prompt, opts)
}

function controller(source, label) {
  return run('controller',
    `Run exactly this shell command (Bash, timeout 600000 ms) and return its JSON stdout ` +
    `fields verbatim (paper_id, phase, status, blocked_reason, scientific_status, ` +
    `scientific_blocker, tasks). Do nothing else:\n\n` +
    sh(`run.py tasks "${source}" --json`),
    { label, phase: 'Review', model: 'haiku', effort: 'low', schema: STATE })
}

function worker(pid, t, prior) {
  return run('worker',
    `One isolated REFEREE task. Judge independently; read nothing but the task prompt ` +
    `(and an image file it names).\n` +
    `1. Read ${t.prompt} (self-contained; read it fully).\n` +
    `2. In ONE Bash call, write your JSON answer (exactly as the prompt specifies) and seal:\n` +
    `cat > "${t.out}" <<'__REFEREE_JSON__'\n<your JSON>\n__REFEREE_JSON__\n` +
    `${sh(`run.py seal ${pid} "${t.id}" "${t.out}"`)}\n` +
    `3. If sealing fails, fix the JSON and repeat step 2 (at most 2 retries). Never invent ` +
    `or weaken evidence to pass validation.\n` +
    (prior ? `A previous attempt at this task failed with: ${prior}\n` : '') +
    `Reply with one line: SEALED, or FAILED: <the exact seal error>.`,
    { label: `${pid}:${t.id}`, phase: 'Review',
      model: t.model === 'haiku' ? 'haiku' : 'sonnet', effort: t.effort || 'medium' })
}

async function review(source) {
  const log_ = [], fails = {}, why = {}
  let st = await controller(source, `tasks:${source}`)
  for (let round = 0; st && round < MAX_ROUNDS; round++) {
    const pid = st.paper_id
    const ids = new Set(st.tasks.map(t => t.id))
    const ready = st.tasks.filter(t => (t.after || []).every(a => !ids.has(a))
                                       && (fails[t.id] || 0) < 2)
    if (!ready.length) break
    log(`${pid}: round ${round + 1}, phase ${st.phase}, ${ready.length} task(s)`)
    const res = await parallel(ready.map(t => () => worker(pid, t, why[t.id])))
    st = await controller(pid, `tasks:${pid}`)
    // Sealed = no longer pending, per the harness. A still-pending task counts as a failure.
    const still = new Set(((st && st.tasks) || []).map(t => t.id))
    let sealed = 0
    ready.forEach((t, i) => {
      if (!still.has(t.id)) { sealed++; return }
      fails[t.id] = (fails[t.id] || 0) + 1
      why[t.id] = String(res[i] || 'worker returned nothing').slice(0, 600)
    })
    const dropped = Object.keys(fails).filter(k => fails[k] >= 2)
    if (dropped.length) log(`${pid}: giving up on ${dropped.join(', ')} after 2 failed rounds`)
    log_.push({ round: round + 1, phase: st && st.phase, sealed, tasks: ready.length })
  }
  return { source, final: st && { paper_id: st.paper_id, phase: st.phase,
           workflow_status: st.status, blocked_reason: st.blocked_reason,
           scientific_status: st.scientific_status || 'NOT_ASSESSED',
           scientific_blocker: st.scientific_blocker || '', left: (st.tasks || []).map(t => t.id) },
           rounds: log_, gave_up: Object.keys(fails).filter(k => fails[k] >= 2) }
}

phase('Review')
const results = await pipeline(A.papers || [], p => review(p))
return results
