import { test, expect } from '@playwright/test'
import { open, nav, h1, fillSignup, submitButton, mockSignup } from './helpers.mjs'

const EMAIL = 'pii.probe@example.com'
const PASSWORD = 'Secretpw9'
const SECRETS = [EMAIL, 'pii.probe', PASSWORD]

/** Collect every console message, including structured arguments. */
function captureConsole(page) {
  const pending = []
  page.on('console', (msg) => {
    pending.push(
      Promise.all(msg.args().map((a) => a.jsonValue().then(JSON.stringify).catch(() => '')))
        .then((args) => `${msg.type()}: ${msg.text()} ${args.join(' ')}`)
        .catch(() => `${msg.type()}: ${msg.text()}`),
    )
  })
  page.on('pageerror', (err) => pending.push(Promise.resolve(`pageerror: ${err.message}`)))
  return async () => {
    const lines = await Promise.all(pending)
    return lines.filter((l) => SECRETS.some((s) => l.includes(s)))
  }
}

async function signupFlow(page, respond) {
  await mockSignup(page, { respond })
  await open(page)
  await nav(page, 'Sign up')
  await fillSignup(page, EMAIL, PASSWORD)
  await submitButton(page).click()
}

test('T10-R1 successful signup logs no email or password', async ({ page }) => {
  const leaks = captureConsole(page)
  await signupFlow(page)
  await expect(h1(page)).toHaveText('Account created')
  await page.waitForTimeout(300)
  expect(await leaks()).toEqual([])
})

test('T10-R2 failed signup (500 and network error) logs no email or password', async ({ page }) => {
  const leaks = captureConsole(page)
  await signupFlow(page, (route) => route.fulfill({ status: 500, body: '' }))
  await page.waitForTimeout(800)
  await page.unrouteAll()
  await signupFlow(page, (route) => route.abort('failed'))
  await page.waitForTimeout(800)
  expect(await leaks()).toEqual([])
})

test('T10-R4 signup still works', async ({ page }) => {
  const calls = await mockSignup(page)
  await open(page)
  await nav(page, 'Sign up')
  await fillSignup(page, EMAIL, PASSWORD)
  await submitButton(page).click()
  await expect(h1(page)).toHaveText('Account created')
  expect(JSON.parse(calls[0])).toMatchObject({ email: EMAIL, password: PASSWORD })
})
