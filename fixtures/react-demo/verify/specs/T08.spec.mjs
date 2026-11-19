import { test, expect } from '@playwright/test'
import { open, nav, outOfViewport, SCREENS } from './helpers.mjs'

const shotDir = process.env.VERIFY_ARTIFACTS ? `${process.env.VERIFY_ARTIFACTS}/screenshots` : null

async function eachScreen(page, width, fn) {
  await page.setViewportSize({ width, height: 800 })
  await open(page)
  for (const screen of SCREENS) {
    await nav(page, screen)
    await fn(screen)
    if (shotDir) await page.screenshot({ path: `${shotDir}/${width}-${screen.replace(/\s/g, '_')}.png`, fullPage: true })
  }
}

const hScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)

test('T08-R1 no horizontal scrolling at 375px', async ({ page }) => {
  const bad = []
  await eachScreen(page, 375, async (s) => {
    const over = await hScroll(page)
    if (over > 0) bad.push(`${s}: overflows by ${over}px`)
  })
  expect(bad).toEqual([])
})

test('T08-R2 controls and text stay inside the 375px viewport', async ({ page }) => {
  const bad = []
  await eachScreen(page, 375, async (s) => bad.push(...(await outOfViewport(page)).map((x) => `${s}: ${x}`)))
  expect(bad).toEqual([])
})

test('T08-R3 desktop (1280px) layout still has no overflow', async ({ page }) => {
  const bad = []
  await eachScreen(page, 1280, async (s) => {
    if ((await hScroll(page)) > 0) bad.push(`${s}: horizontal overflow`)
    bad.push(...(await outOfViewport(page)).map((x) => `${s}: ${x}`))
  })
  expect(bad).toEqual([])
})
