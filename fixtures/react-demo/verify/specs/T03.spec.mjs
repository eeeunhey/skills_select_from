import { test, expect } from '@playwright/test'
import { open, focusInDialog } from './helpers.mjs'

async function openHelp(page) {
  await open(page)
  await page.getByRole('main').getByRole('button', { name: 'Help', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
}

async function focusFirstInDialog(page) {
  await page.evaluate(() => {
    const d = document.querySelector('[role="dialog"], dialog[open]')
    d.querySelector('a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])').focus()
  })
}

test('T03-R1 Escape closes the modal', async ({ page }) => {
  await openHelp(page)
  await focusFirstInDialog(page)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('T03-R2 focus moves into the modal when it opens', async ({ page }) => {
  await openHelp(page)
  await expect.poll(() => focusInDialog(page), { timeout: 2000 }).toBe(true)
})

test('T03-R3 Tab and Shift+Tab keep focus inside the modal', async ({ page }) => {
  await openHelp(page)
  await focusFirstInDialog(page)
  for (const key of ['Tab', 'Shift+Tab']) {
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press(key)
      expect(await focusInDialog(page), `${key} #${i + 1} left the dialog`).toBe(true)
    }
  }
})

test('T03-R4 focus returns to the Help button after closing', async ({ page }) => {
  await openHelp(page)
  await page.getByRole('dialog').getByRole('button', { name: 'Close' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('main').getByRole('button', { name: 'Help', exact: true })).toBeFocused()
})

test('T03-R5 dialog is exposed as modal with an accessible name', async ({ page }) => {
  await openHelp(page)
  const ok = await page.evaluate(() => {
    const d = document.querySelector('[role="dialog"], dialog[open]')
    const modal = d.getAttribute('aria-modal') === 'true' || (d.tagName === 'DIALOG' && d.matches(':modal'))
    const named = !!(d.getAttribute('aria-label') || document.getElementById(d.getAttribute('aria-labelledby') || '')?.textContent)
    return modal && named
  })
  expect(ok).toBe(true)
})

test('T03-R6 Close button still closes the modal', async ({ page }) => {
  await openHelp(page)
  await page.getByRole('dialog').getByRole('button', { name: 'Close' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
})
