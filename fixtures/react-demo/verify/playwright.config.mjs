// Driven by verify/run.mjs; do not run directly without the VERIFY_* env vars.
import { defineConfig } from '@playwright/test'

const port = Number(process.env.VERIFY_PORT || 5199)
const workdir = process.env.VERIFY_WORKDIR
if (!workdir) throw new Error('VERIFY_WORKDIR is required (use verify/run.mjs)')

export default defineConfig({
  testDir: './specs',
  timeout: 30_000,
  workers: 1,
  retries: 0,
  reporter: [['json', { outputFile: process.env.VERIFY_REPORT || 'pw-report.json' }]],
  outputDir: process.env.VERIFY_ARTIFACTS ? `${process.env.VERIFY_ARTIFACTS}/pw` : undefined,
  use: { channel: 'chrome', headless: true, baseURL: `http://127.0.0.1:${port}` },
  webServer: {
    // The fixture's pristine vite.config.js is used, so edits to the working copy's config cannot change the verifier.
    command: `"${process.execPath}" "${process.env.VERIFY_VITE}" "${workdir}" --config "${process.env.VERIFY_VITE_CONFIG}" --port ${port} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
})
