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
        sans: ['Inter', '"Segoe UI"', 'system-ui', 'DengXian', '"Microsoft YaHei"', '"PingFang SC"', 'sans-serif'],
        // 西文放前面是刻意的：字体栈按字符回退，中文照旧往后找，但拉丁字母与数字
        // 交给 Georgia，不再用宋体自带那套窄西文（中英混排时两者基线、字宽都打架）。
        // 中文候选按「装了才用」排：思源宋体 → 华文中宋 → 宋体兜底。
        serif: ['Georgia', 'Cambria', '"Noto Serif SC"', '"Source Han Serif SC"', '"Songti SC"', 'STZhongsong', 'SimSun', 'serif'],
        // 楷体：Windows 的 KaiTi / macOS 的 STKaiti、Kaiti SC。选它是因为全程矢量描边，
        // 不像宋体那样在小字号退回内嵌点阵（实测 ≤17px 时没有任何灰度过渡）。
        kai: ['"Times New Roman"', 'KaiTi', 'STKaiti', '"Kaiti SC"', 'SimSun', 'serif'],
        mono: ['"JetBrains Mono"', 'Consolas', '"Sarasa Term SC"', 'monospace'],
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
