import { test, expect } from '@playwright/test'
import { open, nav, h1, fillSignup, submitButton, mockSignup } from './helpers.mjs'

const INVALID = ['not-an-email', 'jane@', '@example.com', 'jane doe@example.com']
const VALID = ['jane.doe@example.com', 'j.doe+news@mail.example.org']

test('T02-R1 invalid email blocks submission', async ({ page }) => {
  const calls = await mockSignup(page)
  await open(page)
  await nav(page, 'Sign up')
  for (const email of INVALID) {
    await fillSignup(page, email, 'abcdefg1')
    await submitButton(page).click()
    await page.waitForTimeout(400)
    await expect(h1(page), `submitted with "${email}"`).toHaveText('Create account')
  }
  expect(calls.length, 'signup requests sent for invalid emails').toBe(0)
})

test('T02-R2 invalid email shows an accessible error message mentioning the email', async ({ page }) => {
  await mockSignup(page)
  await open(page)
  await nav(page, 'Sign up')
  await fillSignup(page, 'not-an-email', 'abcdefg1')
  await submitButton(page).click()
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const visible = (el) => el && el.offsetParent !== null && /email/i.test(el.textContent || '')
          const alerts = [...document.querySelectorAll('[role="alert"]')].some(visible)
          const input = document.getElementById('email') || document.querySelector('input[type="email"]')
          const ids = (input?.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)
          return alerts || ids.some((id) => visible(document.getElementById(id)))
        }),
      { timeout: 3000 },
    )
    .toBe(true)
})

test('T02-R3 valid emails still submit successfully', async ({ page }) => {
  for (const email of VALID) {
    await page.unrouteAll()
    const calls = await mockSignup(page)
    await open(page)
    await nav(page, 'Sign up')
    await fillSignup(page, email, 'abcdefg1')
    await submitButton(page).click()
    await expect(h1(page), `rejected valid "${email}"`).toHaveText('Account created')
    expect(calls.length).toBe(1)
  }
})
