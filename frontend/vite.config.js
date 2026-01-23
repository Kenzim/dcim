import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true
      }
    },
    // Handle SPA routing - serve index.html for all routes
    historyApiFallback: true
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: undefined
      }
    }
  }
});




