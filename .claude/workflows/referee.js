export const meta = {
  name: 'referee',
  description: 'Autonomous REFEREE review of PDFs: harness advances, isolated Sonnet subagents answer and seal each task',
  whenToUse: 'Review one or more paper PDFs end to end with no human steps',
  phases: [{ title: 'Review', detail: 'per paper: tasks -> isolated workers -> seal, until done' }],
}

// args: { papers: ["papers/x.pdf", ...], repo: "C:/.../single-harness",
//         python: "../.venv/Scripts/python", env: "PYTHONUTF8=1 SH_PROJECTS_DIR=... ...",
//         maxRounds: 12 }
// Model work is done ONLY by Sonnet workers (one isolated context per task) and a Haiku
// controller that runs harness commands. Worker model is clamped to haiku|sonnet: never Opus. The harness validates and seals every answer.
const A = args || {}
const REPO = A.repo
const PY = A.python || '../.venv/Scripts/python'
const ENV = A.env || 'PYTHONUTF8=1'
const MAX_ROUNDS = A.maxRounds || 12
const sh = cmd => `cd "${REPO}" && ${ENV} ${PY} ${cmd}`

const STATE = {
  type: 'object',
  properties: {
    paper_id: { type: 'string' }, phase: { type: 'string' }, status: { type: 'string' },
    blocked_reason: { type: 'string' },
    tasks: { type: 'array', items: { type: 'object', properties: {
      id: { type: 'string' }, role: { type: 'string' }, prompt: { type: 'string' },
      out: { type: 'string' }, model: { type: 'string' }, effort: { type: 'string' },
      after: { type: 'array', items: { type: 'string' } } },
      required: ['id', 'role', 'prompt', 'out', 'model', 'effort', 'after'] } },
  },
  required: ['paper_id', 'phase', 'status', 'tasks'],
}

const RESULT = {
  type: 'object',
  properties: { sealed: { type: 'boolean' }, detail: { type: 'string' } },
  required: ['sealed', 'detail'],
}

function controller(source, label) {
  return agent(
    `Run exactly this shell command (Bash tool, timeout 600000 ms) and return its JSON ` +
    `stdout fields verbatim (paper_id, phase, status, blocked_reason, tasks). Do nothing ` +
    `else, read no other files:\n\n${sh(`run.py tasks "${source}" --json`)}`,
    { label, phase: 'Review', model: 'haiku', effort: 'low', schema: STATE })
}

function worker(pid, t, prior) {
  return agent(
    `You are one isolated reviewer for a single task of an automated paper review.\n` +
    `1. Read the task prompt file: ${t.prompt}\n` +
    `   Follow it exactly. You may read files it names (the paper PDF, figure images). ` +
    `Do NOT read any other review output, prompt, or JSON under the project directory: ` +
    `your judgement must be independent.\n` +
    `2. Write ONLY your final JSON answer (as the prompt specifies) to: ${t.out}\n` +
    `3. Seal it with this command (Bash):\n   ${sh(`run.py seal ${pid} "${t.id}" "${t.out}"`)}\n` +
    `4. If sealing exits non-zero, read the error, fix the JSON, rewrite the file and ` +
    `seal again (at most 2 retries). Never weaken or invent evidence to pass validation.\n` +
    (prior ? `NOTE: a previous attempt at this task failed with: ${prior}\n` +
     `Avoid repeating that failure.\n` : '') +
    `Return sealed=true/false and a one-line detail (on failure: the exact seal error).`,
    { label: `${pid}:${t.id}`, phase: 'Review', schema: RESULT,
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
    ready.forEach((t, i) => {
      if (!(res[i] && res[i].sealed)) {
        fails[t.id] = (fails[t.id] || 0) + 1
        why[t.id] = (res[i] && res[i].detail || 'worker returned nothing').slice(0, 600)
      }
    })
    const dropped = Object.keys(fails).filter(k => fails[k] >= 2)
    if (dropped.length) log(`${pid}: giving up on ${dropped.join(', ')} after 2 failed rounds`)
    log_.push({ round: round + 1, phase: st.phase,
                sealed: res.filter(r => r && r.sealed).length, tasks: ready.length })
    st = await controller(pid, `tasks:${pid}`)
  }
  return { source, final: st && { paper_id: st.paper_id, phase: st.phase, status: st.status,
           blocked_reason: st.blocked_reason, left: (st.tasks || []).map(t => t.id) },
           rounds: log_, gave_up: Object.keys(fails).filter(k => fails[k] >= 2) }
}

phase('Review')
const results = await pipeline(A.papers || [], p => review(p))
return results
