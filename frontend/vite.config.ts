import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Where the backend is. Overridable so a developer running it on another port does not have
// to edit tracked configuration.
const BACKEND = process.env.AETOS_BACKEND ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  // The same proxy for `vite dev` and `vite preview`. Keeping them in step means a preview
  // build talks to the backend exactly as the dev server does, so "works in dev" and "works
  // in the built bundle" cannot quietly diverge.
  server: {
    port: 5173,
    // The backend owns all authoritative state, so every data path goes through it. Proxying
    // in development keeps the frontend origin-relative, which means no CORS configuration
    // and no environment-specific base URL compiled into the bundle.
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true, ws: true },
    },
  },
  preview: {
    port: 4173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true, ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['tests/**/*.test.{ts,tsx}'],
  },
})
