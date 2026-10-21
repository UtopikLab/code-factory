export interface Task {
    task_id: string
    repo: string | null
    issue_number: number | null
    status: string
    lifecycle?: string
    credential_cleanup?: string
    attempts?: number
    retry_of?: string | null
    retry_task_id?: string | null
    config_version?: string | null
    config_hash?: string | null
    pr_url?: string | null
}
export interface TaskPage { tasks: Task[]; total: number; limit: number; offset: number }
export interface Config {
    source: string
    version: string
    hash: string | null
    models: { role: string; model: string; native_download: boolean; availability: string }[]
    agents: { id: string; role: string; endpoint_alias: string; max_files: number; tools: string[]; availability: string }[]
}
export class ApiError extends Error {
    readonly status: number
    constructor(status: number) {
        const messages: Record<number, string> = {
            400: 'Invalid request. Check the repository and issue number.',
            401: 'Sign in is required.', 403: 'Access denied.', 404: 'Task not found.',
            409: 'Task conflicts with an existing attempt.',
            503: 'Management access or server configuration is unavailable.',
        }
        super(messages[status] || 'Request failed. Try again.')
        this.status = status
    }
}
export async function api<Result>(path: string, payload?: unknown): Promise<Result> {
    const response = await fetch(`/management/api${path}`, {
        method: payload === undefined ? 'GET' : 'POST', credentials: 'same-origin', redirect: 'error',
        headers: payload === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Management-Request': '1' },
        body: payload === undefined ? undefined : JSON.stringify(payload),
    })
    if (!response.ok) throw new ApiError(response.status)
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Session response unavailable. Sign in again.')
    return response.json() as Promise<Result>
}