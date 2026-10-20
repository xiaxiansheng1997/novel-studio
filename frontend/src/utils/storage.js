export function getApiKey() {
  return localStorage.getItem('w2a_apikey') || ''
}

export function setApiKey(key) {
  localStorage.setItem('w2a_apikey', key)
}

export function getTheme() {
  return localStorage.getItem('w2a_theme') || 'dark'
}

export function setTheme(t) {
  localStorage.setItem('w2a_theme', t)
  document.documentElement.classList.toggle('dark', t === 'dark')
}
