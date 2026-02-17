/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["/opt/mtproto/app/templates/**/*.html"],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        dark: {
          50: '#f8fafc',
          100: '#e2e8f0',
          200: '#94a3b8',
          300: '#64748b',
          400: '#475569',
          500: '#334155',
          600: '#1e293b',
          700: '#0f172a',
          800: '#0c1222',
          900: '#060a14',
        }
      }
    }
  },
  plugins: [],
}
