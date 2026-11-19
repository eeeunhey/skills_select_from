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
export const submitButton = (page) => page.locator('main form').getByRole('button', { name: /sign up/i })

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

/** All visible elements matching selector that stick out of the viewport horizontally. */
export function outOfViewport(page, selector = 'button, input, h1, label, a, p') {
  return page.evaluate((sel) => {
    const w = window.innerWidth
    return [...document.querySelectorAll(sel)]
      .filter((el) => el.offsetParent !== null)
      .map((el) => ({ el, r: el.getBoundingClientRect() }))
      .filter(({ r }) => r.width > 0 && (r.left < -0.5 || r.right > w + 0.5))
      .map(({ el, r }) => `${el.tagName.toLowerCase()} "${(el.textContent || el.id || '').trim().slice(0, 30)}" [${Math.round(r.left)},${Math.round(r.right)}]`)
  }, selector)
}

export const SCREENS = ['Home', 'Sign up', 'Profile', 'Settings']
