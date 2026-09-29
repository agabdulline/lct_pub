import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Порт и адрес API — под infra/docker-compose.yml (стандартные 5173 и 8000 на машине заняты).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  // Lazy screens pull these in later; pre-bundling them avoids a reload on the first visit.
  optimizeDeps: { include: ['react-force-graph-2d', 'd3-force-3d', 'recharts', 'cmdk', '@tanstack/react-table'] },
  server: {
    port: 15173,
    proxy: { '/api': { target: process.env.VITE_API_PROXY ?? 'http://localhost:18000', changeOrigin: true } },
  },
})
