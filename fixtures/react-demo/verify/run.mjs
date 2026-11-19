#!/usr/bin/env node
// External verifier entry point (PRD F5: runs OUTSIDE the evaluated agent).
//
//   node verify/run.mjs prepare --dest <dir>
//       Copy the app (without verify/ and node_modules/) to <dir> and link node_modules. This is the agent's working copy.
//   node verify/run.mjs --case T01 --workdir <dir> [--json out.json] [--log transcript.jsonl] [--port 5199] [--artifacts <dir>]
//       Grade a working copy. Prints/writes {case_id, requirements:[{id, result, verifier, verifier_kind, detail}], summary}.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { STATIC, changeSummary } from './static.mjs'
import { logPolicy } from './log_policy.mjs'

const VERIFY = path.dirname(fileURLToPath(import.meta.url))
const FIXTURE = path.dirname(VERIFY)
const CASES = JSON.parse(fs.readFileSync(path.join(VERIFY, 'cases.json'), 'utf8'))
const KIND_TO_VERIFIER = { browser: 'external_test', static: 'external_test', unit: 'external_test', log_policy: 'log_policy', human: 'human' }
const EXCLUDE = new Set(['node_modules', 'verify', 'dist', 'test-results', 'playwright-report'])

function args(argv) {
  const out = { _: [] }
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) out[argv[i].slice(2)] = argv[i + 1]?.startsWith('--') || argv[i + 1] === undefined ? true : argv[++i]
    else out._.push(argv[i])
  }
  return out
}

function linkNodeModules(dir) {
  const nm = path.join(dir, 'node_modules')
  if (!fs.existsSync(nm)) fs.symlinkSync(path.join(FIXTURE, 'node_modules'), nm, 'junction')
}

export function prepare(dest) {
  fs.mkdirSync(dest, { recursive: true })
  fs.cpSync(FIXTURE, dest, { recursive: true, filter: (src) => !EXCLUDE.has(path.relative(FIXTURE, src).split(path.sep)[0]) })
  linkNodeModules(dest)
  return dest
}

// Agent-written code runs here (vite build, unit imports): give it no secrets, only what node/chrome need.
const SAFE_ENV_KEYS = ['PATH', 'Path', 'PATHEXT', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'HOME',
  'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData', 'LANG', 'PLAYWRIGHT_BROWSERS_PATH']
export function safeEnv(extra = {}) {
  const env = {}
  for (const k of SAFE_ENV_KEYS) if (process.env[k] !== undefined) env[k] = process.env[k]
  return { ...env, ...extra }
}

function playwrightResults(caseId, workdir, port, artifacts) {
  const report = path.join(artifacts, 'pw-report.json')
  fs.rmSync(report, { force: true })
  const env = safeEnv({
    VERIFY_WORKDIR: workdir,
    VERIFY_PORT: String(port),
    VERIFY_REPORT: report,
    VERIFY_ARTIFACTS: artifacts,
    VERIFY_VITE: path.join(FIXTURE, 'node_modules', 'vite', 'bin', 'vite.js'),
    VERIFY_VITE_CONFIG: path.join(FIXTURE, 'vite.config.js'),
  })
  fs.mkdirSync(path.join(artifacts, 'screenshots'), { recursive: true })
  const cli = path.join(FIXTURE, 'node_modules', '@playwright', 'test', 'cli.js')
  const p = spawnSync(process.execPath, [cli, 'test', '--config', path.join(VERIFY, 'playwright.config.mjs'), `${caseId}.spec.mjs`], {
    cwd: VERIFY,
    env,
    encoding: 'utf8',
    timeout: 300_000,
  })
  const results = {}
  if (!fs.existsSync(report)) return { results, error: `playwright produced no report (exit ${p.status}): ${(p.stderr || p.stdout || '').slice(-800)}` }
  const json = JSON.parse(fs.readFileSync(report, 'utf8'))
  const visit = (suite) => {
    for (const spec of suite.specs || []) {
      const id = spec.title.split(' ')[0]
      const last = spec.tests?.[0]?.results?.at(-1)
      const status = last?.status
      const msg = (last?.errors || []).map((e) => e.message).join('\n').replace(/\u001b\[[0-9;]*m/g, '')
      results[id] = status === 'passed' ? { result: 'pass', detail: spec.title } : { result: 'fail', detail: `${spec.title}: ${status}\n${msg.slice(0, 1200)}` }
    }
    for (const s of suite.suites || []) visit(s)
  }
  for (const s of json.suites || []) visit(s)
  const globalErr = (json.errors || []).map((e) => e.message).join('\n')
  return { results, error: globalErr ? globalErr.slice(0, 800) : null }
}

function humanEvidence(testId, workdir, artifacts) {
  if (testId === 'T08-R4') return `blinded human review of screenshots in ${path.join(artifacts, 'screenshots')}`
  if (testId === 'T04-R3') return 'blinded human review of visual button consistency (1-5 rubric)'
  if (testId === 'T09-R4') return `human review of change size/readability; diffstat vs base: ${changeSummary(workdir, FIXTURE)}`
  return 'requires human review'
}

export function verify({ caseId, workdir, log, port = 5199, artifacts }) {
  const reqs = CASES[caseId]
  if (!reqs) throw new Error(`unknown case ${caseId}`)
  workdir = path.resolve(workdir)
  artifacts = path.resolve(artifacts || fs.mkdtempSync(path.join(os.tmpdir(), `verify-${caseId}-`)))
  fs.mkdirSync(artifacts, { recursive: true })
  linkNodeModules(workdir)

  const kinds = Object.values(reqs)
  const pw = kinds.includes('browser') ? playwrightResults(caseId, workdir, port, artifacts) : { results: {} }

  const requirements = Object.entries(reqs).map(([id, kind]) => {
    const testId = `${caseId}-${id}`
    let r
    if (kind === 'browser') r = pw.results[testId] || { result: 'unevaluated', detail: pw.error || 'no test result reported' }
    else if (kind === 'static') r = (STATIC[testId] || STATIC[id])(workdir, FIXTURE)
    else if (kind === 'unit') {
      const p = spawnSync(process.execPath, [path.join(VERIFY, 'unit', `${caseId}.mjs`), workdir], { encoding: 'utf8', timeout: 30_000, env: safeEnv() })
      r = p.status === null ? { result: 'unevaluated', detail: 'unit test timed out' } : { result: p.status === 0 ? 'pass' : 'fail', detail: (p.stdout + p.stderr).trim().slice(0, 800) }
    } else if (kind === 'log_policy') r = logPolicy(workdir, log)
    else r = { result: 'unevaluated', detail: humanEvidence(testId, workdir, artifacts) }
    return { id, result: r.result, verifier: testId, verifier_kind: KIND_TO_VERIFIER[kind], detail: r.detail }
  })
  const summary = { pass: 0, fail: 0, unevaluated: 0 }
  for (const r of requirements) summary[r.result]++
  return { case_id: caseId, workdir, artifacts, generated_at: new Date().toISOString(), requirements, summary }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const a = args(process.argv.slice(2))
  if (a._[0] === 'prepare') {
    if (!a.dest) throw new Error('prepare needs --dest <dir>')
    console.log(prepare(path.resolve(a.dest)))
  } else {
    if (!a.case || !a.workdir) {
      console.error('usage: node verify/run.mjs --case T01 --workdir <dir> [--json out.json] [--log transcript.jsonl] [--port 5199] [--artifacts dir]\n       node verify/run.mjs prepare --dest <dir>')
      process.exit(2)
    }
    const out = verify({ caseId: a.case, workdir: a.workdir, log: a.log, port: Number(a.port || 5199), artifacts: a.artifacts })
    const text = JSON.stringify(out, null, 2)
    if (a.json) fs.writeFileSync(a.json, text)
    console.log(text)
  }
}
