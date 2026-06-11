import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react-swc'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    middlewareMode: false,
    // Videos are served from frontend/public/videos/ by Vite — no proxy needed.
    // /api/* calls use absolute URLs (see src/api.js baseURL), so no proxy needed either.
    fs: {
      allow: ['..']
    }
  },
  // Configure static file serving
  build: {
    rollupOptions: {
      output: {
        assetFileNames: 'assets/[name]-[hash][extname]'
      }
    }
  }
})
