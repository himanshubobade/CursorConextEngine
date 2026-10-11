import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In dev the UI runs on 5173 and proxies the API to the FastAPI process.
// `vite build` emits to web/dist, which run.py serves from the same origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8100',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
