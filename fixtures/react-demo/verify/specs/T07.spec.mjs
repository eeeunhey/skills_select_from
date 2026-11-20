import { test, expect } from '@playwright/test'
import { open, nav, h1, fillSignup, submitButton, mockSignup } from './helpers.mjs'

const RAW = /\b(500|503)\b|request failed:\s*\d|typeerror|failed to fetch|stack|undefined|\[object/i

async function failWith(page, respond) {
  await mockSignup(page, { respond })
  await open(page)
  await nav(page, 'Sign up')
  await fillSignup(page, 'jane.doe@example.com', 'abcdefg1')
  await submitButton(page).click()
}

async function userMessage(page) {
  const alert = page.getByRole('alert')
  await expect(alert.first()).toBeVisible({ timeout: 3000 })
  const text = (await alert.allInnerTexts()).join(' ').trim()
  expect(text.length, 'message too short').toBeGreaterThanOrEqual(10)
  expect(text, 'message exposes a raw technical error').not.toMatch(RAW)
  await expect(h1(page)).toHaveText('Create account')
}

test('T07-R1 server error (HTTP 500) shows a human-readable message', async ({ page }) => {
  await failWith(page, (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' }))
  await userMessage(page)
})

test('T07-R2 network failure shows a human-readable message', async ({ page }) => {
  await failWith(page, (route) => route.abort('failed'))
  await userMessage(page)
})

test('T07-R3 entered values are preserved after an error', async ({ page }) => {
  await failWith(page, (route) => route.fulfill({ status: 500, body: '' }))
  await page.waitForTimeout(500)
  await expect(page.getByLabel('Email', { exact: true })).toHaveValue('jane.doe@example.com')
})
