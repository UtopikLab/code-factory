import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Management from './Management'

const task = { task_id: 'task-1', repo: 'org/repo', issue_number: 7, status: 'queued', attempts: 0, lifecycle: 'pending' }
let calls: [string, RequestInit | undefined][]
let taskResponse: unknown
let status = 200

beforeEach(() => {
    window.location.hash = '/tasks'
    calls = []; status = 200; taskResponse = { tasks: [task], total: 1, limit: 25, offset: 0 }
    vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
        calls.push([url, options])
        const body = url.endsWith('/session') ? { subject: 'operator' } : url.endsWith('/repositories') ? { repositories: ['org/repo'] }
            : options?.method === 'POST' ? task : url.includes('/tasks/task-1') ? { ...task, status: 'completed', pr_url: 'https://github.com/org/repo/pull/8' }
                : url.includes('/config') ? { version: '1', hash: 'abc', models: [{ role: 'coder', model: 'verified-model', native_download: true, availability: 'unknown' }], agents: [] } : taskResponse
        return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    }))
})
afterEach(() => vi.unstubAllGlobals())

function mount() {
    return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><Management /></QueryClientProvider>)
}

test('renders tasks, filters, and the safe task detail', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(await screen.findByRole('link', { name: 'task-1' }))
    expect(await screen.findByRole('complementary', { name: 'Task detail' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Human review' })).toHaveAttribute('href', 'https://github.com/org/repo/pull/8')
    await user.selectOptions(screen.getByLabelText('Status'), 'running')
    await waitFor(() => expect(calls.some(([url]) => url.includes('status=running'))).toBe(true))
})

test('submits only repository and issue', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(await screen.findByRole('button', { name: 'New task' }))
    expect(screen.getByRole('button', { name: 'Submit task' })).toBeDisabled()
    await screen.findByRole('option', { name: 'org/repo' })
    await user.selectOptions(screen.getByLabelText('Repository', { selector: 'select' }), 'org/repo')
    await user.type(screen.getByLabelText('Issue number'), '7')
    await user.click(screen.getByRole('button', { name: 'Submit task' }))
    await waitFor(() => expect(calls.some(([, options]) => options?.method === 'POST')).toBe(true))
    const post = calls.find(([, options]) => options?.method === 'POST')!
    expect(JSON.parse(post[1]!.body as string)).toEqual({ repo: 'org/repo', issue_number: 7 })
    expect(post[1]!.headers).not.toHaveProperty('Authorization')
    expect(post[1]!.headers).toHaveProperty('X-Management-Request', '1')
})

test('has honest empty and read-only model states', async () => {
    taskResponse = { tasks: [], total: 0, limit: 25, offset: 0 }
    const user = userEvent.setup()
    mount()
    expect(await screen.findByText('No tasks found.')).toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: 'Models' }))
    expect(await screen.findByText('verified-model')).toBeInTheDocument()
    expect(screen.getByText('unknown')).toBeInTheDocument()
    expect(screen.getByText('Read only')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /save|deploy/i })).not.toBeInTheDocument()
})

test('denied session never loads management data', async () => {
    status = 401
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign in is required.')
    expect(calls).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'New task' })).not.toBeInTheDocument()
})