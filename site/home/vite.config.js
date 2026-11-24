import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  root: resolve(__dirname, 'src_site'),
  publicDir: resolve(__dirname, 'public'),
  base: './',
  build: {
    outDir: resolve(__dirname, 'site'),
    emptyOutDir: true,
    assetsInlineLimit: 4096,
    rollupOptions: {
      output: {
        manualChunks: {
          three: ['three'],
          motion: ['gsap', 'lenis'],
        },
      },
    },
  },
  server: {
    port: 4173,
    host: '127.0.0.1',
  },
});
