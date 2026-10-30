/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // 色名一律转成 CSS 变量：单一来源，主题切换只改变量值，不再维护第二份 hex。
        // 代价是这些色名不能用 Tailwind 的透明度修饰符（如 bg-ink-900/50）—— 全仓已核，无人这么写。
        ink: {
          950: 'var(--bg)',
          900: 'var(--canvas)',
          800: 'var(--panel-2)',
          700: 'var(--hover)',
          600: 'var(--panel-3)',
          500: 'var(--panel-4)',
        },
        line: {
          soft: 'var(--line-soft)',
          DEFAULT: 'var(--line)',
          strong: 'var(--line-strong)',
        },
        txt: {
          DEFAULT: 'var(--text)',
          muted: 'var(--text-muted)',
          faint: 'var(--text-faint)',
        },
        run: {
          DEFAULT: 'var(--run)',
          dim: 'var(--run-dim)',
        },
        info: 'var(--info)',
        iris: 'var(--iris)',
        warn: 'var(--warn)',
        danger: 'var(--danger)',
      },
      fontFamily: {
        sans: ['Inter', '"Segoe UI"', 'system-ui', '"Microsoft YaHei"', 'sans-serif'],
        // 写作正文与标题用衬线，中文用宋体兜底
        serif: ['"Noto Serif SC"', '"Source Han Serif SC"', '"Songti SC"', 'SimSun', 'serif'],
        mono: ['"JetBrains Mono"', 'Consolas', 'monospace'],
      },
      borderRadius: {
        control: '8px',
        panel: '12px',
        stage: '16px',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        pop: 'var(--shadow-pop)',
        focus: 'var(--ring)',
      },
    },
  },
  plugins: [],
}
