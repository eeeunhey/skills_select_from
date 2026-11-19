// T09-R1: src/lib/validation.js exports validatePassword(password) -> message string | null.
// Usage: node verify/unit/T09.mjs <workdir>   (exit 0 = pass)
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const MSG = 'Password must be at least 8 characters and include a number.'
const CASES = [
  ['', MSG],
  ['abc', MSG],
  ['abcdefgh', MSG],
  ['1234567', MSG],
  ['short1', MSG],
  ['abcdefg1', null],
  ['12345678', null],
  ['a1b2c3d4e5', null],
]

const file = path.join(process.argv[2], 'src', 'lib', 'validation.js')
let mod
try {
  mod = await import(pathToFileURL(file).href)
} catch (e) {
  console.log(`cannot import src/lib/validation.js: ${e.message}`)
  process.exit(1)
}
if (typeof mod.validatePassword !== 'function') {
  console.log('validatePassword is not exported')
  process.exit(1)
}
const bad = CASES.filter(([pw, want]) => (mod.validatePassword(pw) ?? null) !== want).map(([pw, want]) => `"${pw}": expected ${JSON.stringify(want)}, got ${JSON.stringify(mod.validatePassword(pw))}`)
console.log(bad.length ? bad.join('\n') : `${CASES.length} cases ok`)
process.exit(bad.length ? 1 : 0)
