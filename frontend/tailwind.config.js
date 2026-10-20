/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // 页面底色：由深到浅，用于分层而不是堆阴影
        ink: {
          950: '#070A10',
          900: '#0B1018',
          800: '#0F1621',
          700: '#141D2A',
          600: '#1A2534',
          500: '#223044',
        },
        // 描边：soft 用于同层分隔，DEFAULT 用于卡片，strong 用于聚焦/悬浮
        line: {
          soft: '#16202D',
          DEFAULT: '#1E2A3A',
          strong: '#2C3B50',
        },
        txt: {
          DEFAULT: '#E8EEF7',
          muted: '#A3B0C2',
          faint: '#6C7C92',
        },
        // 主色：代码暗底 + 运行绿
        run: {
          DEFAULT: '#22C55E',
          dim: '#16A34A',
          soft: 'rgba(34, 197, 94, 0.12)',
        },
        info: '#38BDF8',
        iris: '#8B5CF6',
        warn: '#F59E0B',
        danger: '#F87171',
      },
      fontFamily: {
        sans: ['"Fira Sans"', '"Segoe UI"', 'system-ui', '"Microsoft YaHei"', 'sans-serif'],
        mono: ['"Fira Code"', 'Consolas', '"JetBrains Mono"', 'monospace'],
      },
      borderRadius: {
        control: '10px',
        panel: '14px',
        stage: '18px',
      },
      boxShadow: {
        card: 'inset 0 1px 0 rgba(255,255,255,0.03), 0 24px 48px -32px rgba(0,0,0,0.9)',
        pop: '0 18px 40px -16px rgba(0,0,0,0.85)',
        focus: '0 0 0 2px rgba(34,197,94,0.35)',
      },
    },
  },
  plugins: [],
}
