import { expect } from '@playwright/test'

export async function open(page) {
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
}

/** Navigate with the top navigation bar. */
export async function nav(page, name) {
  await page.getByRole('navigation').getByRole('button', { name, exact: true }).click()
}

export const h1 = (page) => page.getByRole('heading', { level: 1 })
// by type, not label: a correct fix may relabel the button while pending ("Signing up…")
export const submitButton = (page) => page.locator('main form button[type=submit]')

export async function fillSignup(page, email, password) {
  await page.getByLabel('Email', { exact: true }).fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
}

/** Count POSTs to /api/signup; respond via `respond(route)` (default: 201 after `delay` ms). */
export async function mockSignup(page, { delay = 0, respond } = {}) {
  const calls = []
  await page.route('**/api/signup', async (route) => {
    if (route.request().method() === 'POST') calls.push(route.request().postData())
    if (delay) await new Promise((r) => setTimeout(r, delay))
    if (respond) return respond(route)
    return route.fulfill({ status: 201, contentType: 'application/json', body: '{"ok":true}' })
  })
  return calls
}

/** Is document.activeElement inside the (single) dialog? */
export function focusInDialog(page) {
  return page.evaluate(() => {
    const d = document.querySelector('[role="dialog"], dialog[open]')
    return !!d && d.contains(document.activeElement) && document.activeElement !== document.body
  })
}

export const TEXT_ELEMENTS = 'button, input, h1, h2, h3, label, a, p, span, li'

/** All visible elements matching selector that stick out of the viewport horizontally. */
export function outOfViewport(page, selector = TEXT_ELEMENTS) {
  return page.evaluate((sel) => {
    const w = window.innerWidth
    return [...document.querySelectorAll(sel)]
      .filter((el) => el.offsetParent !== null)
      .map((el) => ({ el, r: el.getBoundingClientRect() }))
      .filter(({ r }) => r.width > 0 && (r.left < -0.5 || r.right > w + 0.5))
      .map(({ el, r }) => `${el.tagName.toLowerCase()} "${(el.textContent || el.id || '').trim().slice(0, 30)}" [${Math.round(r.left)},${Math.round(r.right)}]`)
  }, selector)
}

/** Visible text elements cut off horizontally: by their own overflow (ellipsis) or by an ancestor whose overflow-x is not visible. */
export function clippedText(page, selector = TEXT_ELEMENTS) {
  return page.evaluate((sel) => {
    const out = []
    for (const el of document.querySelectorAll(sel)) {
      if (el.offsetParent === null || el.tagName === 'INPUT' || !el.textContent.trim()) continue
      const r = el.getBoundingClientRect()
      if (!r.width) continue
      const label = `${el.tagName.toLowerCase()} "${el.textContent.trim().slice(0, 30)}"`
      if (getComputedStyle(el).overflowX !== 'visible' && el.scrollWidth > el.clientWidth + 1) out.push(`${label} clipped by its own overflow`)
      for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) {
        if (getComputedStyle(a).overflowX === 'visible') continue
        const ar = a.getBoundingClientRect()
        if (r.left < ar.left - 0.5 || r.right > ar.right + 0.5) out.push(`${label} clipped by ${a.tagName.toLowerCase()}.${a.className}`)
      }
    }
    return out
  }, selector)
}

/** "tag|text" keys of the visible text elements (display/visibility/zero size count as hidden). */
export function visibleTextKeys(page, selector = TEXT_ELEMENTS) {
  return page.evaluate((sel) =>
    [...document.querySelectorAll(sel)]
      .filter((el) => {
        if (el.offsetParent === null || getComputedStyle(el).visibility !== 'visible') return false
        const r = el.getBoundingClientRect()
        return r.width > 0 && r.height > 0
      })
      .map((el) => `${el.tagName.toLowerCase()}|${(el.textContent || el.id || '').trim().slice(0, 40)}`),
  selector)
}

export const SCREENS = ['Home', 'Sign up', 'Profile', 'Settings']
