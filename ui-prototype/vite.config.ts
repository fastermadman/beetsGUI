import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Fixed port so Onlook (and the README) can point at one address.
// To try Preact later: add resolve.alias { react: "preact/compat", "react-dom": "preact/compat" }.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
});
