import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const dir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    // Exact match so ships/web/shipLayer.js (outside this package) can import
    // the peer three. A string alias would also swallow three/addons.
    alias: [
      { find: /^three$/, replacement: path.resolve(dir, "node_modules/three/build/three.module.js") },
    ],
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    fs: {
      allow: [dir, path.resolve(dir, "..")],
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 5173,
  },
});
