/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // 墨色分层：近黑到抬升，写作时长时间注视不刺眼
        ink: {
          950: '#0B0C0F',
          900: '#111318',
          800: '#15181E',
          700: '#1B1F26',
          600: '#22262F',
          500: '#2A2F39',
        },
        // 描边：soft 同层分隔，DEFAULT 卡片，strong 聚焦/悬浮
        line: {
          soft: '#1A1E25',
          DEFAULT: '#262B34',
          strong: '#363C48',
        },
        // 文字：暖白而不是纯白，降低蓝光刺激
        txt: {
          DEFAULT: '#ECE9E4',
          muted: '#A9A6A0',
          faint: '#6F6C67',
        },
        // 主色：墨金。写作工具要安静、有质感，不用荧光绿
        run: {
          DEFAULT: '#D9A45B',
          dim: '#B8863F',
          soft: 'rgba(217, 164, 91, 0.12)',
        },
        info: '#7E9CF7',
        iris: '#9B8CF0',
        warn: '#E0A44A',
        danger: '#E47878',
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
        card: 'inset 0 1px 0 rgba(255,255,255,0.03), 0 24px 48px -32px rgba(0,0,0,0.9)',
        pop: '0 18px 40px -16px rgba(0,0,0,0.85)',
        focus: '0 0 0 2px rgba(217,164,91,0.32)',
      },
    },
  },
  plugins: [],
}
