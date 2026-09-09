import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  server: {
    allowedHosts: ['rackflow.lan.stackken.com', 'rackflow.stackken.com'],
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://localhost:8000',
        changeOrigin: true,
        // Required for /api/vnc/ws and /api/kvm/ws: without this, Vite's
        // dev proxy never upgrades the WebSocket handshake.
        ws: true
      }
    },
    // Handle SPA routing - serve index.html for all routes
    historyApiFallback: true
  },
  build: {
    // es2022 (top-level await) is required by @novnc/novnc's feature
    // detection; supported by all evergreen browsers since 2022.
    target: 'es2022',
    rollupOptions: {
      output: {
        manualChunks: undefined
      }
    }
  },
  optimizeDeps: {
    // Vite's dev-time dependency pre-bundler defaults to an older esbuild
    // target than `build.target` above, which fails on @novnc/novnc's
    // top-level await feature detection. Match it here too.
    esbuildOptions: {
      target: 'es2022'
    }
  }
});




