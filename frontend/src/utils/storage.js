export function getApiKey() {
  return localStorage.getItem('w2a_apikey') || ''
}

export function setApiKey(key) {
  localStorage.setItem('w2a_apikey', key)
}

// 主题相关函数已移到 utils/theme（那里是 w2a_theme 的唯一读写处）
