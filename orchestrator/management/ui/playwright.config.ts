import { defineConfig } from '@playwright/test'

export default defineConfig({
    testDir: './e2e',
    use: { baseURL: 'http://127.0.0.1:5174', browserName: 'chromium' },
    projects: [
        { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
        { name: 'mobile', use: { viewport: { width: 390, height: 844 }, isMobile: true } },
    ],
    webServer: { command: 'npm run dev -- --port 5174 --strictPort', url: 'http://127.0.0.1:5174', reuseExistingServer: !process.env.CI },
})