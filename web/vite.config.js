import { resolve } from 'node:path';
import { defineConfig } from 'vite';

const page = (name) => resolve(import.meta.dirname, name);

/*
 * CSP stricte : aucun script, style, police ou connexion hors de l'origine du site.
 * GitHub Pages ne permet pas d'en-têtes HTTP personnalisés : la politique est posée
 * en balise <meta> dans chaque page, au build uniquement (le serveur de dev de Vite
 * injecte des styles en ligne). Si vous activez un outil de mesure d'audience
 * externe, ajoutez son domaine à script-src et connect-src.
 */
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data: blob:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join('; ');

const strictCsp = () => ({
  name: 'aiobot-strict-csp',
  apply: 'build',
  transformIndexHtml: {
    order: 'post',
    handler: () => [
      { tag: 'meta', attrs: { 'http-equiv': 'Content-Security-Policy', content: CSP }, injectTo: 'head-prepend' },
      { tag: 'meta', attrs: { name: 'referrer', content: 'strict-origin-when-cross-origin' }, injectTo: 'head-prepend' },
    ],
  },
});

export default defineConfig({
  plugins: [strictCsp()],
  build: {
    target: 'es2022',
    sourcemap: false,
    // Three.js (~145 ko gzip) est chargé à la demande, après le premier rendu.
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      input: {
        index: page('index.html'),
        mentions: page('mentions-legales.html'),
        confidentialite: page('confidentialite.html'),
        cookies: page('cookies.html'),
      },
    },
  },
});
