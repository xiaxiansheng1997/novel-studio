import { getApiKey } from './storage'

// 受保护接口统一入口：/v1、/admin、/live 共用同一个 W2A_API_KEY。
// 服务端没设口令时 getApiKey() 为空，这里就是普通 fetch，不额外带头。
export function authHeaders(extra = {}) {
  const headers = new Headers(extra)
  const key = getApiKey()
  if (key) headers.set('Authorization', 'Bearer ' + key)
  return headers
}

export function apiFetch(url, opts = {}) {
  return fetch(url, { ...opts, headers: authHeaders(opts.headers || {}) })
}
