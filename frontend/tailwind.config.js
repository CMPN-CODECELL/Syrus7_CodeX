const c = (v) => `rgb(var(--${v}) / <alpha-value>)`

export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg: c('bg'), panel: c('panel'), panel2: c('panel2'), line: c('line'), fg: c('fg'),
        muted: c('muted'), faint: c('faint'), up: c('up'), down: c('down'), accent: c('accent'), warn: c('warn'),
      },
    },
  },
  plugins: [],
}
