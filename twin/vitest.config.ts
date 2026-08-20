import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    // The simulation layer is deliberately free of DOM and three.js dependencies, so it
    // tests in plain node. That separation is the point: the part of this prototype that
    // carries the reasoning can be verified without rendering anything.
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
