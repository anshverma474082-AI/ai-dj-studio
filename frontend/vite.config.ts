import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The console talks to one origin. Vite proxies /api, /media and /ws to the FastAPI server so
// the browser never needs a CORS exception, the audio the catalog points at resolves from the same
// place the API serves it, and the WebSocket the app opens is the server's real one.
export default defineConfig({
  plugins: [react()],
  server: {
    // Not the Vite default: 5173 is left free for whatever else is already running on this
    // machine, so the console has one address that does not depend on who started first.
    port: 5174,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000' },
      '/media': { target: 'http://127.0.0.1:8000' },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
});
