import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', proxy: { '/management/api': 'http://127.0.0.1:8081', '/oauth2': 'http://127.0.0.1:8081' } },
  test: { environment: 'jsdom', setupFiles: './src/test-setup.ts', include: ['src/**/*.test.tsx'] },
})
