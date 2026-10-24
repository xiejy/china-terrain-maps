import { resolve } from 'node:path';
import { defineConfig } from 'vite';

// Relative base so the site works under a GitHub project-pages path.
// appType 'mpa': missing data files must 404 instead of falling back to index.html.
export default defineConfig({
  base: './',
  appType: 'mpa',
  build: {
    target: 'es2022',
    chunkSizeWarningLimit: 1500,
    rolldownOptions: {
      input: {
        index: resolve(import.meta.dirname, 'index.html'),
        city: resolve(import.meta.dirname, 'city.html'),
      },
      output: {
        // Name the big vendor chunks so the bundle report is readable; three.js stays lazy-loaded.
        codeSplitting: {
          groups: [
            { name: 'maplibre', test: /node_modules[\\/](maplibre-gl|@maplibre|pmtiles|fflate)/ },
            { name: 'three', test: /node_modules[\\/]three/ },
          ],
        },
      },
    },
  },
});
