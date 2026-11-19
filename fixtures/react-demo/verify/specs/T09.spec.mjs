import { test, expect } from '@playwright/test'
import { open, nav, h1, fillSignup, submitButton, mockSignup } from './helpers.mjs'

const MSG = 'Password must be at least 8 characters and include a number.'

test('T09-R3 both password forms behave exactly as before', async ({ page }) => {
  await mockSignup(page)
  await open(page)

  await nav(page, 'Sign up')
  for (const pw of ['short1', 'abcdefgh', '']) {
    await fillSignup(page, 'jane.doe@example.com', pw)
    await submitButton(page).click()
    await expect(page.getByRole('alert'), `signup pw="${pw}"`).toHaveText(MSG)
  }
  await fillSignup(page, 'jane.doe@example.com', 'abcdefg1')
  await submitButton(page).click()
  await expect(h1(page)).toHaveText('Account created')

  await nav(page, 'Profile')
  const save = page.locator('main form').getByRole('button', { name: 'Save' })
  for (const pw of ['short', 'abcdefgh']) {
    await page.getByLabel('New password').fill(pw)
    await save.click()
    await expect(page.getByRole('alert'), `profile pw="${pw}"`).toHaveText(MSG)
  }
  for (const pw of ['abcdefg1', '']) {
    await page.getByLabel('New password').fill(pw)
    await save.click()
    await expect(page.getByRole('status'), `profile pw="${pw}"`).toHaveText('Saved.')
  }
})
