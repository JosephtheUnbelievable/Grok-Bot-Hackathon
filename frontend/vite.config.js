import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const dir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
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
