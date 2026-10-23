import { useState, useSyncExternalStore } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as Dialog from '@radix-ui/react-dialog'
import { Activity, ArrowLeft, ArrowRight, Bot, Check, ChevronRight, CircuitBoard, ExternalLink, GitPullRequest, ListTodo, LoaderCircle, LockKeyhole, Plus, RefreshCw, ShieldCheck, X } from 'lucide-react'
import { api, ApiError } from './api'
import type { Config, Task, TaskPage } from './api'

function subscribeRoute(callback: () => void) {
    window.addEventListener('hashchange', callback)
    return () => window.removeEventListener('hashchange', callback)
}
function routeSnapshot() { return window.location.hash || '#/tasks' }

function Status({ value }: { value: string }) {
    return <span className={`status status-${value}`}><span aria-hidden="true" />{value}</span>
}

function QueryState({ loading, error, empty, retry }: { loading?: boolean; error?: Error | null; empty?: string; retry: () => void }) {
    if (loading) return <div className="state" role="status"><LoaderCircle className="spin" />Loading</div>
    if (error) return <div className="state error" role="alert"><span>{error.message}</span><button onClick={retry}><RefreshCw />Retry</button></div>
    return <div className="state">{empty}</div>
}

function IconButton({ label, children, onClick, disabled }: { label: string; children: ReactNode; onClick: () => void; disabled?: boolean }) {
    return <button type="button" className="icon-button" aria-label={label} title={label} onClick={onClick} disabled={disabled}>{children}</button>
}

function Submission({ onSubmitted }: { onSubmitted: (task: Task) => void }) {
    const [open, setOpen] = useState(false)
    const [repo, setRepo] = useState('')
    const [issue, setIssue] = useState('')
    const repositories = useQuery({ queryKey: ['repositories'], queryFn: () => api<{ repositories: string[] }>('/repositories'), enabled: open, retry: false })
    const github = useQuery({ queryKey: ['github-session'], queryFn: () => api<{ connected: boolean; configured: boolean }>('/github/session'), enabled: open, retry: false })
    const connection = useMutation({
        mutationFn: () => api<{ authorization_url: string }>('/github/connect', {}),
        onSuccess: data => { window.location.assign(data.authorization_url) }
    })
    const disconnection = useMutation({
        mutationFn: () => api('/github/disconnect', {}),
        onSuccess: () => { void github.refetch() }
    })
    const submission = useMutation({
        mutationFn: (payload: { repo: string; issue_number: number }) => api<Task>('/tasks', payload),
        onSuccess: task => { setOpen(false); setIssue(''); onSubmitted(task) }
    })
    const dynamic = repositories.data?.repositories.length === 0
    const validRepo = dynamic ? /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) : (repositories.data?.repositories.includes(repo) ?? false)
    const valid = github.data?.connected && validRepo && /^[1-9][0-9]*$/.test(issue) && Number(issue) <= 2147483647
    function submit(event: FormEvent) {
        event.preventDefault()
        if (valid && !submission.isPending) submission.mutate({ repo, issue_number: Number(issue) })
    }
    return <Dialog.Root open={open} onOpenChange={value => { if (!submission.isPending) { setOpen(value); submission.reset() } }}>
        <Dialog.Trigger asChild><button className="primary"><Plus />New task</button></Dialog.Trigger>
        <Dialog.Portal><Dialog.Overlay className="overlay" /><Dialog.Content className="dialog" aria-describedby={undefined}
            onEscapeKeyDown={event => { if (submission.isPending) event.preventDefault() }}>
            <div className="dialog-heading"><Dialog.Title>Submit task</Dialog.Title><Dialog.Close asChild><button className="icon-button" aria-label="Close submission" title="Close submission" disabled={submission.isPending}><X /></button></Dialog.Close></div>
            <form onSubmit={submit}>
                {github.data?.connected ? <button type="button" onClick={() => disconnection.mutate()} disabled={disconnection.isPending}><ShieldCheck />Disconnect GitHub</button>
                    : <button type="button" onClick={() => connection.mutate()} disabled={!github.data?.configured || connection.isPending}><GitPullRequest />Connect GitHub</button>}
                {github.data && !github.data.configured && <p role="status">GitHub login is not configured.</p>}
                {(github.error || connection.error || disconnection.error) && <p role="alert" className="error">{(github.error || connection.error || disconnection.error)?.message}</p>}
                <label htmlFor="repository">Repository</label>
                {dynamic ? <input id="repository" placeholder="owner/repository" value={repo} onChange={event => setRepo(event.target.value)} required disabled={submission.isPending} /> : <select id="repository" value={repo} onChange={event => setRepo(event.target.value)} required disabled={submission.isPending || repositories.isPending}>
                    <option value="">Select repository</option>{repositories.data?.repositories.map(name => <option key={name}>{name}</option>)}
                </select>}
                {repositories.isPending && <p role="status">Loading repositories</p>}
                {repositories.isError && <QueryState error={repositories.error} retry={() => { void repositories.refetch() }} />}
                <label htmlFor="issue">Issue number</label>
                <input id="issue" type="number" min="1" max="2147483647" step="1" inputMode="numeric" value={issue} onChange={event => setIssue(event.target.value)} required disabled={submission.isPending} />
                {submission.isError && <p className="error" role="alert">{submission.error.message}</p>}
                <div className="form-actions"><Dialog.Close asChild><button type="button" disabled={submission.isPending}>Cancel</button></Dialog.Close><button className="primary" disabled={!valid || submission.isPending}>{submission.isPending ? <LoaderCircle className="spin" /> : <Plus />}{submission.isPending ? 'Submitting' : 'Submit task'}</button></div>
            </form>
        </Dialog.Content></Dialog.Portal>
    </Dialog.Root>
}

function TaskDetail({ taskId }: { taskId: string }) {
    const query = useQuery({ queryKey: ['task', taskId], queryFn: () => api<Task>(`/tasks/${encodeURIComponent(taskId)}`), refetchInterval: 10000 })
    const task = query.data
    return <aside className="detail" aria-label="Task detail">
        <div className="section-heading"><h2>Task detail</h2><a className="icon-button" href="#/tasks" aria-label="Close task detail" title="Close task detail"><X /></a></div>
        {query.isPending || query.isError || !task ? <QueryState loading={query.isPending} error={query.error} retry={() => { void query.refetch() }} /> : <>
            <div className="detail-status"><Status value={task.status} /><span className="muted">Runtime</span></div>
            <h3 className="task-identifier">{task.task_id}</h3>
            <dl>
                <dt>Repository</dt><dd>{task.repo || 'Not recorded'}</dd>
                <dt>Issue</dt><dd>{task.repo && task.issue_number ? <a href={`https://github.com/${task.repo}/issues/${task.issue_number}`} target="_blank" rel="noreferrer">#{task.issue_number}<ExternalLink /></a> : 'Not recorded'}</dd>
                <dt>Attempts</dt><dd>{task.attempts ?? 'Not recorded'}</dd>
                <dt>Lifecycle</dt><dd>{task.lifecycle || 'Not recorded'}</dd>
                <dt>Credential cleanup</dt><dd>{task.credential_cleanup || 'Not recorded'}</dd>
                <dt>Retry of</dt><dd>{task.retry_of ? <a href={`#/tasks/${task.retry_of}`}>{task.retry_of}</a> : 'None'}</dd>
                <dt>Next attempt</dt><dd>{task.retry_task_id ? <a href={`#/tasks/${task.retry_task_id}`}>{task.retry_task_id}</a> : 'None'}</dd>
            </dl>
            {task.pr_url && <a className="review-link" href={task.pr_url} target="_blank" rel="noreferrer"><GitPullRequest />Human review<ExternalLink /></a>}
            <h3 className="subheading">Configuration snapshot</h3><dl><dt>Version</dt><dd>{task.config_version || 'Not recorded'}</dd><dt>Hash</dt><dd className="mono">{task.config_hash || 'Not recorded'}</dd></dl>
        </>}
    </aside>
}

function Tasks({ taskId }: { taskId?: string }) {
    const [status, setStatus] = useState('')
    const [repo, setRepo] = useState('')
    const [filter, setFilter] = useState('')
    const [offset, setOffset] = useState(0)
    const client = useQueryClient()
    const params = new URLSearchParams({ limit: '25', offset: String(offset) })
    if (status) params.set('status', status)
    if (filter) params.set('repo', filter)
    const query = useQuery({ queryKey: ['tasks', status, filter, offset], queryFn: () => api<TaskPage>(`/tasks?${params}`), refetchInterval: 10000 })
    function submitted(task: Task) {
        setStatus(''); setRepo(''); setFilter(''); setOffset(0)
        void client.invalidateQueries({ queryKey: ['tasks'] })
        void client.invalidateQueries({ queryKey: ['task', task.task_id] })
        window.location.hash = `/tasks/${task.task_id}`
    }
    return <>
        <header className="page-heading"><div><div className="eyebrow"><Activity />Runtime</div><h1>Tasks</h1></div><Submission onSubmitted={submitted} /></header>
        <div className="toolbar">
            <label className="filter-label">Status<select value={status} onChange={event => { setStatus(event.target.value); setOffset(0) }}><option value="">All statuses</option>{['queued', 'running', 'completed', 'failed'].map(value => <option key={value}>{value}</option>)}</select></label>
            <form className="repo-filter" onSubmit={event => { event.preventDefault(); setFilter(repo.trim()); setOffset(0) }}>
                <label className="filter-label" htmlFor="repo-filter">Repository<input id="repo-filter" placeholder="owner/repository" value={repo} onChange={event => setRepo(event.target.value)} /></label>
                <IconButton label="Apply repository filter" onClick={() => { setFilter(repo.trim()); setOffset(0) }}><Check /></IconButton>
            </form>
            {(status || filter) && <button onClick={() => { setStatus(''); setRepo(''); setFilter(''); setOffset(0) }}><X />Clear</button>}
            <div className="toolbar-end"><span className="muted" role="status">{query.isFetching ? 'Updating' : '10s refresh'}</span><IconButton label="Refresh tasks" onClick={() => { void query.refetch() }} disabled={query.isFetching}><RefreshCw className={query.isFetching ? 'spin' : ''} /></IconButton></div>
        </div>
        <div className={`task-workspace ${taskId ? 'with-detail' : ''}`}>
            <section className="task-list" aria-label="Task list">
                {query.isPending || query.isError ? <QueryState loading={query.isPending} error={query.error} retry={() => { void query.refetch() }} /> : query.data.tasks.length === 0 ? <QueryState empty={offset ? 'No tasks on this page.' : 'No tasks found.'} retry={() => { void query.refetch() }} /> :
                    <table><thead><tr><th>Task / repository</th><th>Issue</th><th>Status</th><th className="wide-column">Attempts</th><th className="wide-column">Lifecycle</th><th><span className="sr-only">Open</span></th></tr></thead><tbody>{query.data.tasks.map(task =>
                        <tr key={task.task_id} className={taskId === task.task_id ? 'selected' : ''}><td><a className="task-link mono" title={task.task_id} href={`#/tasks/${task.task_id}`}>{task.task_id.length > 40 ? `${task.task_id.slice(0, 40)}...` : task.task_id}</a><span className="repository-name">{task.repo || 'Not recorded'}</span></td><td>{task.issue_number ? `#${task.issue_number}` : '-'}</td><td><Status value={task.status} /></td><td className="wide-column mono">{task.attempts ?? '-'}</td><td className="wide-column">{task.lifecycle}</td><td><a className="row-open" href={`#/tasks/${task.task_id}`} aria-label={`Open task ${task.task_id}`} title="Open task"><ChevronRight /></a></td></tr>)}</tbody></table>}
                <footer className="pagination"><span>{query.data && !query.isError ? `${query.data.total === 0 ? 0 : offset + 1}-${Math.min(offset + 25, query.data.total)} of ${query.data.total}` : '-'}</span><div><IconButton label="Previous page" onClick={() => setOffset(Math.max(0, offset - 25))} disabled={offset === 0 || query.isPending}><ArrowLeft /></IconButton><IconButton label="Next page" onClick={() => setOffset(offset + 25)} disabled={!query.data || query.isError || offset + 25 >= query.data.total || query.isFetching}><ArrowRight /></IconButton></div></footer>
            </section>
            {taskId && <TaskDetail key={taskId} taskId={taskId} />}
        </div>
    </>
}

function Configuration({ view }: { view: 'models' | 'agents' }) {
    const query = useQuery({ queryKey: ['config'], queryFn: () => api<Config>('/config') })
    return <>
        <header className="page-heading"><div><div className="eyebrow"><ShieldCheck />Git / Helm managed</div><h1>{view === 'models' ? 'Models' : 'Agents'}</h1></div><span className="read-only"><LockKeyhole />Read only</span></header>
        {query.isPending || query.isError ? <QueryState loading={query.isPending} error={query.error} retry={() => { void query.refetch() }} /> : <>
            <div className="config-meta"><span>Version <strong>{query.data.version}</strong></span><span className="config-hash mono" title={query.data.hash || undefined}>{query.data.hash || 'Hash not recorded'}</span><IconButton label="Refresh configuration" onClick={() => { void query.refetch() }} disabled={query.isFetching}><RefreshCw /></IconButton></div>
            {view === 'models' ? <div className="table-scroll"><table><thead><tr><th>Configured model</th><th>Role</th><th>Native download</th><th>Live availability</th></tr></thead><tbody>{query.data.models.map(model => <tr key={model.role}><td><div className="identity-cell"><CircuitBoard /><span className="mono">{model.model || 'Not configured'}</span></div></td><td>{model.role}</td><td>{model.native_download ? 'Enabled' : 'Disabled'}</td><td><Status value="unknown" /></td></tr>)}</tbody></table></div> :
                query.data.agents.length === 0 ? <QueryState empty="No catalog agents configured." retry={() => { void query.refetch() }} /> : <div className="table-scroll"><table><thead><tr><th>Catalog agent</th><th>Endpoint alias</th><th>Tools</th><th>File limit</th><th>Live availability</th></tr></thead><tbody>{query.data.agents.map(agent => <tr key={agent.role}><td><div className="identity-cell"><Bot /><span>{agent.id}</span></div></td><td className="mono">{agent.endpoint_alias}</td><td><div className="tool-list">{agent.tools.map(tool => <span key={tool} className="mono">{tool}</span>)}</div></td><td>{agent.max_files}</td><td><Status value="unknown" /></td></tr>)}</tbody></table></div>}
        </>}
    </>
}

export default function Management() {
    const route = useSyncExternalStore(subscribeRoute, routeSnapshot)
    const [, view = 'tasks', taskId] = route.split('/')
    const activeView = view === 'models' || view === 'agents' ? view : 'tasks'
    const session = useQuery({ queryKey: ['session'], queryFn: () => api<{ subject: string }>('/session'), retry: false, refetchInterval: 60000 })
    if (session.isPending) return <main className="access-state"><LoaderCircle className="spin" /><h1>Code Factory</h1><p role="status">Checking access</p></main>
    if (session.isError) return <main className="access-state"><LockKeyhole /><h1>Code Factory</h1><p role="alert">{session.error.message}</p>{session.error instanceof ApiError && session.error.status === 401 && <a href="/oauth2/start?rd=/">Sign in</a>}<button onClick={() => { void session.refetch() }}><RefreshCw />Check access</button></main>
    return <div className="app-shell"><a className="skip-link" href="#main">Skip to content</a>
        <div className="topbar"><a className="brand" href="#/tasks"><CircuitBoard /><span>Code Factory</span></a><span className="operator"><ShieldCheck />Operator</span></div>
        <div className="app-body"><nav aria-label="Management"><div className="nav-label">Workspace</div>{[
            { id: 'tasks', name: 'Tasks', icon: ListTodo }, { id: 'models', name: 'Models', icon: CircuitBoard }, { id: 'agents', name: 'Agents', icon: Bot },
        ].map(item => <a key={item.id} href={`#/${item.id}`} aria-current={activeView === item.id ? 'page' : undefined}><item.icon />{item.name}</a>)}<div className="nav-footer"><GitPullRequest />Human PR gate</div></nav>
            <main id="main" tabIndex={-1}>{activeView === 'tasks' ? <Tasks taskId={taskId} /> : <Configuration view={activeView} />}</main>
        </div>
    </div>
}