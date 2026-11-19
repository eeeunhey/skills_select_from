import { test, expect } from '@playwright/test'
import { open, nav, h1, fillSignup, submitButton, mockSignup } from './helpers.mjs'

async function ready(page, delay) {
  const calls = await mockSignup(page, { delay })
  await open(page)
  await nav(page, 'Sign up')
  await fillSignup(page, 'jane.doe@example.com', 'abcdefg1')
  return calls
}

test('T06-R1 double-click sends exactly one signup request', async ({ page }) => {
  const calls = await ready(page, 800)
  const box = await submitButton(page).boundingBox()
  await page.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2)
  await page.waitForTimeout(1500)
  expect(calls.length).toBe(1)
})

test('T06-R2 submit button is disabled while the request is pending', async ({ page }) => {
  await ready(page, 1500)
  await submitButton(page).click()
  await expect
    .poll(() => submitButton(page).evaluate((b) => b.disabled || b.getAttribute('aria-disabled') === 'true'), { timeout: 800 })
    .toBe(true)
})

test('T06-R3 a single submit still reaches the success screen', async ({ page }) => {
  const calls = await ready(page, 200)
  await submitButton(page).click()
  await expect(h1(page)).toHaveText('Account created')
  expect(calls.length).toBe(1)
})
