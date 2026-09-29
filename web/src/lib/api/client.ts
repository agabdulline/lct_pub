// API client: REST over fetch, or the mock transport when VITE_API_MOCK=1.
// The mock is imported dynamically, so it never gets into a normal build.

export const MOCK = import.meta.env.VITE_API_MOCK === '1'

const REQUEST_TIMEOUT_MS = 20_000

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

export type QueryValue = string | number | boolean | null | undefined | (string | number)[]
export type QueryParams = Record<string, QueryValue>

export function toSearchParams(query?: QueryParams): URLSearchParams {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value)) {
      if (value.length) params.set(key, value.join(','))
    } else params.set(key, String(value))
  }
  return params
}

export function apiUrl(path: string, query?: QueryParams): string {
  const qs = toSearchParams(query).toString()
  return `/api/v1${path}${qs ? `?${qs}` : ''}`
}

async function request<T>(method: string, path: string, query?: QueryParams, body?: unknown, timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  if (MOCK) {
    const { mockRequest, MockHttpError } = await import('./mock')
    try {
      return (await mockRequest(method, path, toSearchParams(query), body)) as T
    } catch (e) {
      if (e instanceof MockHttpError) throw new ApiError(e.status, e.code, e.message)
      throw e
    }
  }
  // A hung API must not leave a screen in loading forever.
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetch(apiUrl(path, query), {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
  } catch (e) {
    if (controller.signal.aborted) throw new ApiError(0, 'timeout', `API не ответил за ${timeoutMs / 1000} с`)
    throw new ApiError(0, 'network', `API недоступен: ${(e as Error).message}`)
  } finally {
    clearTimeout(timer)
  }
  if (!res.ok) {
    let code = 'http_error'
    let message = `${res.status} ${res.statusText}`
    try {
      const data = (await res.json()) as { error?: { code: string; message: string }; detail?: unknown }
      if (data.error) ({ code, message } = data.error)
      else if (typeof data.detail === 'string') message = data.detail
    } catch {
      // not JSON — keep the status text
    }
    throw new ApiError(res.status, code, message)
  }
  if (res.status === 204) return undefined as T
  const type = res.headers.get('content-type') ?? ''
  return (type.includes('application/json') ? await res.json() : await res.text()) as T
}

export const apiGet = <T>(path: string, query?: QueryParams) => request<T>('GET', path, query)
export const apiPost = <T>(path: string, body?: unknown, query?: QueryParams) => request<T>('POST', path, query, body ?? {})
export const apiPatch = <T>(path: string, body: unknown, query?: QueryParams) => request<T>('PATCH', path, query, body)
export const apiPut = <T>(path: string, body: unknown, timeoutMs?: number) => request<T>('PUT', path, undefined, body, timeoutMs)

async function errorOf(status: number, text: string, fallback: string): Promise<ApiError> {
  try {
    const data = JSON.parse(text) as { error?: { code: string; message: string } }
    if (data.error) return new ApiError(status, data.error.code, data.error.message)
  } catch {
    // not JSON
  }
  return new ApiError(status, 'http_error', fallback)
}

/**
 * A file as multipart `file`, with upload progress (0..1): journals are up to 300 MB, fetch cannot report it.
 * The mock reads the file in the browser.
 */
export async function apiUpload<T>(path: string, file: File, onProgress?: (share: number) => void): Promise<T> {
  if (MOCK) {
    const { mockUpload, MockHttpError } = await import('./mock')
    try {
      return (await mockUpload(path, file, onProgress)) as T
    } catch (e) {
      if (e instanceof MockHttpError) throw new ApiError(e.status, e.code, e.message)
      throw e
    }
  }
  const form = new FormData()
  form.append('file', file)
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', apiUrl(path))
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total)
    xhr.onerror = () => reject(new ApiError(0, 'network', 'API недоступен: файл не отправлен'))
    xhr.onload = async () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve(JSON.parse(xhr.responseText) as T)
      // A proxy in front of the API answers 413 with its own HTML page — say it in words, not «413 Request Entity Too Large»
      const fallback = xhr.status === 413 ? 'Сервер не принял файл: он больше допустимого размера' : `${xhr.status} ${xhr.statusText}`
      reject(await errorOf(xhr.status, xhr.responseText, fallback))
    }
    xhr.send(form)
  })
}

/** Downloads a text resource (CSV export) as a file — works the same with the mock. */
export async function apiDownload(path: string, query: QueryParams | undefined, filename: string) {
  const text = await request<string>('GET', path, query)
  const url = URL.createObjectURL(new Blob(['﻿', text], { type: 'text/csv;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

/** EventSource for /stream, or its mock. */
export async function openEventSource(query: QueryParams): Promise<EventSource> {
  const url = apiUrl('/stream', query)
  if (MOCK) {
    const { MockEventSource } = await import('./mock')
    return new MockEventSource(url) as unknown as EventSource
  }
  return new EventSource(url)
}
