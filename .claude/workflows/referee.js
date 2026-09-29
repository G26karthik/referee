export const meta = {
  name: 'referee',
  description: 'Autonomous REFEREE review of PDFs: harness advances, isolated Sonnet subagents answer and seal each task',
  whenToUse: 'Review one or more paper PDFs end to end with no human steps',
  phases: [{ title: 'Review', detail: 'per paper: tasks -> isolated workers -> seal, until done' }],
}

// args: { papers: ["papers/x.pdf" | "<paper-id>", ...], repo: "C:/.../single-harness",
//         python: "../.venv/Scripts/python", env: "PYTHONUTF8=1 SH_PROJECTS_DIR=... ...", maxRounds: 30,
//         workerModel?: "claude-sonnet-5-5", controllerModel?: "haiku" }
// Model work is done ONLY by isolated Sonnet workers (one fresh context per task) and a Haiku
// controller that runs the harness command — never Opus. The worker model is pinned (it makes
// every scientific judgment, so a run records exactly which model made them); the controller
// only relays JSON, so it follows the current Haiku alias. Whether a task sealed is read back
// from the harness's own task list, never from the worker's say-so. Token budget: every
// worker turn re-sends its context, so the protocol is ~3 turns: one message of parallel
// Reads over the harness's ranges, Write the answer, one Bash seal.
const A = args || {}
const REPO = A.repo
const PY = A.python || '../.venv/Scripts/python'
const ENV = A.env || 'PYTHONUTF8=1'
// Rounds: lenses -> critic -> plan -> per-check bind/gen/verify (+1 revision) -> report,
// plus polls while experiments run in the background (each poll waits up to 9 minutes).
const MAX_ROUNDS = A.maxRounds || 30
const WORKER_MODEL = A.workerModel || 'claude-sonnet-5-5'
const CONTROLLER_MODEL = A.controllerModel || 'haiku'
const sh = cmd => `cd "${REPO}" && ${ENV} ${PY} ${cmd}`

const STATE = {
  type: 'object',
  properties: {
    paper_id: { type: 'string' }, phase: { type: 'string' }, status: { type: 'string' },
    blocked_reason: { type: 'string' }, scientific_status: { type: 'string' },
    tasks: { type: 'array', items: { type: 'object', properties: {
      id: { type: 'string' }, role: { type: 'string' }, prompt: { type: 'string' },
      out: { type: 'string' }, effort: { type: 'string' },
      reads: { type: 'array', items: { type: 'array' } } },
      required: ['id', 'role', 'prompt', 'out', 'effort', 'reads'] } },
  },
  required: ['paper_id', 'phase', 'status', 'tasks'],
}

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
    `Run exactly this shell command (Bash, timeout 600000 ms) and return its JSON stdout fields ` +
    `verbatim (paper_id, phase, status, blocked_reason, scientific_status, tasks). Do nothing else:\n\n` +
    sh(`run.py tasks "${source}" --json --wait 540`),
    { label, phase: 'Review', model: CONTROLLER_MODEL, effort: 'low', schema: STATE })
}

function worker(pid, t, prior) {
  const reads = (t.reads || []).map(([f, o, n]) => `${f} (offset ${o}, limit ${n})`).join('\n  ')
  return run('worker',
    `One isolated REFEREE task. Judge independently.\n` +
    `1. In ONE message, make these parallel Read calls (the task file first):\n  ${reads}\n` +
    `   Read nothing else except files the task itself tells you to open (page images, the ` +
    `authors' checkout).\n` +
    `2. Create every file with the Write tool, never a shell heredoc or echo. Write your JSON answer, ` +
    `exactly as the task specifies, to ${t.out}. Seal only after that Write succeeded. ` +
    `Name any script you draft ${t.id.replace(/[^\w.-]+/g, '_')}.py.\n` +
    `3. Use Bash ONLY for the draft-run command the task gives you (if any; Bash timeout 600000 ms) and to seal: ` +
    `${sh(`run.py seal ${pid} "${t.id}" "${t.out}"`)}\n` +
    `4. If sealing is refused, fix exactly what it names (copy the paper's parsed text exactly), ` +
    `Write again and re-seal (at most 2 retries). Never invent or weaken evidence to pass.\n` +
    (prior ? `A previous attempt at this task failed with: ${prior}\n` : '') +
    `Reply with one line: SEALED, or FAILED: <the exact seal error>.`,
    { label: `${pid}:${t.id}`, phase: 'Review', model: WORKER_MODEL, effort: t.effort || 'medium' })
}

async function review(source) {
  const rounds = [], fails = {}, why = {}
  let st = await controller(source, `tasks:${source}`)
  for (let round = 0; st && round < MAX_ROUNDS; round++) {
    const pid = st.paper_id
    const ready = st.tasks.filter(t => (fails[t.id] || 0) < 2)
    if (!ready.length) {
      if (!String(st.blocked_reason || '').startsWith('executions running')) break
      log(`${pid}: ${st.blocked_reason}; waiting`)
      st = await controller(pid, `wait:${pid}`)
      continue
    }
    log(`${pid}: round ${round + 1}, phase ${st.phase}, ${ready.length} task(s)`)
    const res = await parallel(ready.map(t => () => worker(pid, t, why[t.id])))
    st = await controller(pid, `tasks:${pid}`)
    const still = new Set(((st && st.tasks) || []).map(t => t.id))
    let sealed = 0
    ready.forEach((t, i) => {
      if (!still.has(t.id)) { sealed++; return }
      fails[t.id] = (fails[t.id] || 0) + 1
      why[t.id] = String(res[i] || 'worker returned nothing').slice(0, 600)
    })
    rounds.push({ round: round + 1, phase: st && st.phase, sealed, tasks: ready.length })
  }
  return { source, models: { worker: WORKER_MODEL, controller: CONTROLLER_MODEL }, final: st && { paper_id: st.paper_id, phase: st.phase, workflow_status: st.status,
           blocked_reason: st.blocked_reason, scientific_status: st.scientific_status || 'NOT_ASSESSED',
           left: (st.tasks || []).map(t => t.id) },
           rounds, gave_up: Object.keys(fails).filter(k => fails[k] >= 2) }
}

phase('Review')
return await pipeline(A.papers || [], p => review(p))
