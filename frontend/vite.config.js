import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  plugins: [vue()],
  server: { port: 3202, proxy: { "/api": "http://localhost:8202" } },
});
