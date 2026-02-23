// Optional frontend API host (e.g. http://localhost:8000).
// Keep empty to use same-origin requests in browser.
const rawApiHost = String(import.meta.env.VITE_API_HOST || '').trim();
export const API_HOST = rawApiHost.replace(/\/+$/, '');
