import { test, expect } from '@playwright/test'
import { open, nav, SCREENS } from './helpers.mjs'

// Every rendered button must take its background from a button token and its radius from --radius-md.
function offenders(page, where) {
  return page.evaluate((where) => {
    const probe = document.createElement('div')
    document.body.appendChild(probe)
    const tokenColor = (name) => {
      probe.style.background = `var(${name})`
      return getComputedStyle(probe).backgroundColor
    }
    const allowed = ['--color-primary', '--color-secondary', '--color-danger'].map(tokenColor)
    probe.style.borderRadius = 'var(--radius-md)'
    const radius = getComputedStyle(probe).borderTopLeftRadius
    probe.remove()
    return [...document.querySelectorAll('button')]
      .filter((b) => b.offsetParent !== null)
      .map((b) => ({ b, s: getComputedStyle(b) }))
      .filter(({ s }) => !allowed.includes(s.backgroundColor) || s.borderTopLeftRadius !== radius)
      .map(({ b, s }) => `${where}: "${b.textContent.trim()}" bg=${s.backgroundColor} radius=${s.borderTopLeftRadius} (allowed ${allowed.join(' | ')}; radius ${radius})`)
  }, where)
}

test('T04-R2 all rendered buttons use button design tokens', async ({ page }) => {
  await open(page)
  const bad = []
  for (const screen of SCREENS) {
    await nav(page, screen)
    bad.push(...(await offenders(page, screen)))
  }
  await nav(page, 'Home')
  await page.getByRole('main').getByRole('button', { name: 'Help', exact: true }).click()
  bad.push(...(await offenders(page, 'Help modal')))
  expect(bad).toEqual([])
})
