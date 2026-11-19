import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Tiny mock backend so `npm run dev` works without a server.
function mockApi() {
  return {
    name: 'mock-api',
    configureServer(server) {
      server.middlewares.use('/api', (req, res) => {
        setTimeout(() => {
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ ok: true }))
        }, 300)
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), mockApi()],
})
