import react from "@vitejs/plugin-react";
import { defineConfig, lazyPlugins } from "vite-plus";

export default defineConfig({
  plugins: lazyPlugins(() => [react()]),
  server: {
    host: "127.0.0.1",
    port: 8791,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8790", changeOrigin: true } },
    fs: { deny: ["**/.local/**", "**/*.token", "**/.env*"] },
  },
  preview: { host: "127.0.0.1", port: 8791, strictPort: true },
  fmt: {},
  lint: {
    plugins: ["react", "typescript", "oxc"],
    rules: {
      "react/rules-of-hooks": "error",
      "react/only-export-components": ["warn", { allowConstantExport: true }],
      "vite-plus/prefer-vite-plus-imports": "error",
    },
    options: { typeAware: true, typeCheck: true },
    jsPlugins: [{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" }],
  },
  test: { include: ["src/**/*.test.{ts,tsx}", "server/**/*.test.ts"], environment: "node" },
});
