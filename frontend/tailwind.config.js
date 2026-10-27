/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        douyin: '#161823',
        xhs: '#ff2442',
        // 品牌主色（蓝）：与 Tailwind blue 对齐，语义化引用
        primary: {
          50: '#e6faf8',
          100: '#c5f3ee',
          200: '#8ee6dc',
          500: '#00b8a9',
          600: '#00a396',
          700: '#00857b',
        },
      },
      boxShadow: {
        card: '0 1px 3px rgba(15, 23, 42, 0.06), 0 1px 2px rgba(15, 23, 42, 0.04)',
        pop: '0 8px 24px rgba(15, 23, 42, 0.12)',
      },
      borderRadius: {
        card: '0.75rem',
      },
    },
  },
  plugins: [],
  corePlugins: {
    preflight: false,
  },
}
