// Static (no-browser) checks. Each returns { result: 'pass'|'fail', detail }.
import fs from 'node:fs'
import path from 'node:path'

const read = (p) => fs.readFileSync(p, 'utf8').replace(/\r\n/g, '\n')
const exists = (p) => fs.existsSync(p)

export function walk(dir, exts) {
  if (!exists(dir)) return []
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) return e.name === 'node_modules' ? [] : walk(p, exts)
    return !exts || exts.includes(path.extname(e.name)) ? [p] : []
  })
}

const rel = (root, p) => path.relative(root, p).split(path.sep).join('/')
const ok = (pass, detail) => ({ result: pass ? 'pass' : 'fail', detail })

// --- shared -------------------------------------------------------------
const PROTECTED = ['package.json', 'package-lock.json', 'vite.config.js', 'index.html']

function protectedFiles(work, fixture) {
  const changed = PROTECTED.filter((f) => !exists(path.join(work, f)) || read(path.join(work, f)) !== read(path.join(fixture, f)))
  return ok(changed.length === 0, changed.length ? `modified/removed: ${changed.join(', ')}` : 'protected files unchanged')
}

// --- T04: no raw color / radius literals outside tokens.css ----------------
const COLOR = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/
function tokenLiterals(work) {
  const src = path.join(work, 'src')
  const bad = []
  for (const f of walk(src, ['.css'])) {
    if (rel(src, f) === 'styles/tokens.css') continue
    read(f).split('\n').forEach((line, i) => {
      if (COLOR.test(line) || /border-radius\s*:\s*\d/.test(line)) bad.push(`${rel(work, f)}:${i + 1}: ${line.trim().slice(0, 80)}`)
    })
  }
  for (const f of walk(src, ['.js', '.jsx'])) {
    for (const m of read(f).matchAll(/style=\{\{([\s\S]*?)\}\}/g)) {
      if (COLOR.test(m[1]) || /borderRadius\s*:\s*['"]?\d/.test(m[1])) bad.push(`${rel(work, f)}: inline style {{${m[1].trim().slice(0, 60)}}}`)
    }
  }
  return ok(bad.length === 0, bad.length ? bad.join('\n') : 'no raw color/radius literals outside tokens.css')
}

// --- T04: base token values unchanged (new tokens allowed) -----------------
const customProps = (css) => Object.fromEntries([...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/(--[\w-]+)\s*:\s*([^;}]*)/g)].map((m) => [m[1], m[2].trim().replace(/\s+/g, ' ')]))
function tokenValuesKept(work, fixture) {
  const f = path.join(work, 'src/styles/tokens.css')
  if (!exists(f)) return ok(false, 'src/styles/tokens.css not found')
  const base = customProps(read(path.join(fixture, 'src/styles/tokens.css')))
  const now = customProps(read(f))
  const bad = Object.entries(base).filter(([k, v]) => now[k] !== v).map(([k, v]) => `${k}: ${v} -> ${now[k] ?? '(removed)'}`)
  return ok(bad.length === 0, bad.length ? bad.join('\n') : `all ${Object.keys(base).length} base token values unchanged`)
}

// --- T05: UNUSED.md vs ground truth -----------------------------------------
const GT_UNUSED = ['Avatar', 'LegacyBanner', 'Spinner', 'UserCard']
const GT_USED = ['Button', 'Modal', 'Tooltip']
function listed(work) {
  const f = path.join(work, 'UNUSED.md')
  if (!exists(f)) return null
  // first src/components/<Name>.jsx path on each line = one listed component (later mentions are reasons)
  return new Set(
    read(f)
      .split('\n')
      .map((line) => line.match(/src\/components\/([A-Za-z0-9_]+)\.jsx\b/)?.[1])
      .filter(Boolean),
  )
}
function unusedRecall(work) {
  const l = listed(work)
  if (!l) return ok(false, 'UNUSED.md not found')
  const missing = GT_UNUSED.filter((c) => !l.has(c))
  return ok(missing.length === 0, missing.length ? `missing: ${missing.join(', ')}` : `all ${GT_UNUSED.length} unused components listed`)
}
function unusedPrecision(work) {
  const l = listed(work)
  if (!l) return ok(false, 'UNUSED.md not found')
  const wrong = [...l].filter((c) => !GT_UNUSED.includes(c))
  return ok(wrong.length === 0, wrong.length ? `listed but not unused: ${wrong.join(', ')}` : 'no false positives')
}
function srcUnchanged(work, fixture) {
  const a = walk(path.join(fixture, 'src')).map((f) => rel(fixture, f))
  const b = new Set(walk(path.join(work, 'src')).map((f) => rel(work, f)))
  const diff = a.filter((f) => !b.has(f) || read(path.join(work, f)) !== read(path.join(fixture, f)))
  const added = [...b].filter((f) => !a.includes(f))
  const all = [...diff.map((f) => `changed/removed ${f}`), ...added.map((f) => `added ${f}`)]
  return ok(all.length === 0, all.length ? all.join('\n') : 'src/ untouched')
}

// --- T09: password rule exists only in src/lib/validation.js ----------------
function noDuplicateRule(work) {
  const src = path.join(work, 'src')
  if (!exists(path.join(src, 'lib/validation.js'))) return ok(false, 'src/lib/validation.js not found')
  const bad = []
  for (const f of walk(src, ['.js', '.jsx'])) {
    if (rel(src, f) === 'lib/validation.js') continue
    const t = read(f)
    if (/\.length\s*(<|>=)\s*8\b/.test(t) || /at least 8 characters/.test(t)) bad.push(rel(work, f))
  }
  return ok(bad.length === 0, bad.length ? `rule still duplicated in: ${bad.join(', ')}` : 'rule lives in one module')
}

// --- T10: console calls must not receive PII-bearing identifiers -------------
const PII_IDS = /\b(email|password|body|user|values|formData)\b/
function consoleArgs(text) {
  const out = []
  for (const m of text.matchAll(/console\.(log|info|debug|warn|error|trace|table|dir)\s*\(/g)) {
    let depth = 1
    let i = m.index + m[0].length
    const start = i
    for (; i < text.length && depth; i++) {
      if (text[i] === '(') depth++
      else if (text[i] === ')') depth--
    }
    out.push(text.slice(start, i - 1))
  }
  return out
}
function stripLiterals(s) {
  return s
    .replace(/'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"/g, "''")
    .replace(/`(?:\\.|[^`\\])*`/g, (t) => (t.match(/\$\{[^}]*\}/g) || []).join(' '))
}
function consolePii(work) {
  const bad = []
  for (const f of walk(path.join(work, 'src'), ['.js', '.jsx'])) {
    for (const a of consoleArgs(read(f))) if (PII_IDS.test(stripLiterals(a))) bad.push(`${rel(work, f)}: console(${a.trim().slice(0, 60)})`)
  }
  return ok(bad.length === 0, bad.length ? bad.join('\n') : 'no console call receives email/password/body/user values (heuristic)')
}

// --- human-review evidence (never graded automatically) -------------------
export function changeSummary(work, fixture) {
  const a = walk(path.join(fixture, 'src')).map((f) => rel(fixture, f))
  const b = walk(path.join(work, 'src')).map((f) => rel(work, f))
  const lines = (p) => (exists(p) ? read(p).split('\n') : [])
  const parts = []
  for (const f of new Set([...a, ...b])) {
    const x = lines(path.join(fixture, f))
    const y = lines(path.join(work, f))
    if (x.join('\n') === y.join('\n')) continue
    const xs = new Set(x)
    const ys = new Set(y)
    parts.push(`${f}: +${y.filter((l) => !xs.has(l)).length} -${x.filter((l) => !ys.has(l)).length}`)
  }
  return parts.length ? parts.join('; ') : 'no src changes'
}

export const STATIC = {
  'T04-R1': tokenLiterals,
  'T04-S3': tokenValuesKept,
  'T05-R1': unusedRecall,
  'T05-R2': unusedPrecision,
  'T05-R3': srcUnchanged,
  'T09-R2': noDuplicateRule,
  'T10-R3': consolePii,
  S2: protectedFiles,
}

export { GT_UNUSED, GT_USED }
