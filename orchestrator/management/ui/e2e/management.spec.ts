import { test, expect } from '@playwright/test'

const task = {
    task_id: 'issue-12345678901234567890123456789012', repo: 'example/verified-repository',
    issue_number: 128, status: 'completed', attempts: 2, lifecycle: 'synced', credential_cleanup: 'deleted',
    config_version: '1', config_hash: 'a'.repeat(64), pr_url: 'https://github.com/example/verified-repository/pull/129'
}
const config = {
    version: '1', hash: 'a'.repeat(64), source: 'Git/Helm',
    models: [{ role: 'coder', model: 'Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M', native_download: true, availability: 'unknown' }],
    agents: [{ role: 'coder', id: 'coder', endpoint_alias: 'coder', max_files: 8, tools: ['repository_read', 'candidate_edit', 'sandbox_validation'], availability: 'unknown' }]
}

test('tasks, detail, submission and read-only views fit the viewport', async ({ page }, testInfo) => {
    const failures: string[] = []
    page.on('pageerror', error => failures.push(error.message))
    let submitted: unknown
    await page.route('**/management/api/**', async route => {
        const url = new URL(route.request().url())
        let body: unknown = {}
        if (url.pathname.endsWith('/session')) body = { subject: 'test-operator' }
        else if (url.pathname.endsWith('/config')) body = config
        else if (url.pathname.endsWith('/repositories')) body = { repositories: [task.repo] }
        else if (route.request().method() === 'POST') { submitted = route.request().postDataJSON(); body = task }
        else if (url.pathname.endsWith(`/${task.task_id}`)) body = task
        else body = { tasks: [task], total: 1, limit: 25, offset: 0 }
        await route.fulfill({ json: body })
    })
    await page.goto('/')
    await page.getByRole('link', { name: `Open task ${task.task_id}` }).click()
    await expect(page.getByRole('link', { name: 'Human review' })).toBeVisible()
    await page.evaluate(() => document.fonts.ready)
    await expect(page.locator('body')).not.toHaveJSProperty('scrollWidth', 0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('tasks-detail.png'), fullPage: true })
    await page.getByRole('button', { name: 'New task' }).click()
    await page.getByLabel('Repository', { exact: true }).selectOption(task.repo)
    await page.getByLabel('Issue number').fill('128')
    await expect(page.getByRole('dialog')).toBeVisible()
    expect(await page.getByRole('dialog').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('submission.png'), fullPage: true })
    await page.getByRole('button', { name: 'Submit task', exact: true }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    expect(submitted).toEqual({ repo: task.repo, issue_number: 128 })
    await page.getByRole('link', { name: 'Models', exact: true }).click()
    await expect(page.getByText('Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M')).toBeVisible()
    await expect(page.getByText('unknown', { exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('models.png'), fullPage: true })
    await page.getByRole('link', { name: 'Agents', exact: true }).click()
    await expect(page.getByText('sandbox_validation')).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('agents.png'), fullPage: true })
    expect(failures).toEqual([])
    expect(await page.evaluate(() => localStorage.length)).toBe(0)
})

test('access denied state never renders task controls', async ({ page }, testInfo) => {
    await page.route('**/management/api/**', route => route.fulfill({ status: 401, json: { detail: 'identity-required' } }))
    await page.goto('/')
    await expect(page.getByRole('alert')).toHaveText('Sign in is required.')
    await expect(page.getByRole('button', { name: 'New task' })).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath('access-required.png'), fullPage: true })
})