import { test, expect } from '@playwright/test'
import { open, nav, h1 } from './helpers.mjs'

test('T01-R1 home "View my profile" opens the Profile screen', async ({ page }) => {
  await open(page)
  await page.getByRole('main').getByRole('button', { name: 'View my profile' }).click()
  await expect(h1(page)).toHaveText('Profile')
})

test('T01-R2 other navigation targets unchanged', async ({ page }) => {
  await open(page)
  const expected = { Home: 'Welcome', 'Sign up': 'Create account', Profile: 'Profile', Settings: 'Settings' }
  for (const [button, heading] of Object.entries(expected)) {
    await nav(page, button)
    await expect(h1(page)).toHaveText(heading)
  }
  await nav(page, 'Home')
  await page.getByRole('main').getByRole('button', { name: 'Create account' }).click()
  await expect(h1(page)).toHaveText('Create account')
  await nav(page, 'Settings')
  await page.getByRole('button', { name: 'Back to home' }).click()
  await expect(h1(page)).toHaveText('Welcome')
})
