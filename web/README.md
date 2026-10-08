# Site vitrine AIOBot

Site statique (Vite + Three.js + GSAP) qui présente AIOBot en scène 3D pilotée par le défilement.
Publié sur <https://albanfredon23.github.io/aiobot/> par `.github/workflows/pages.yml` à chaque push sur `main`
touchant `web/`, et servi par le conteneur `aiobot-web` avec `docker compose up --build`.

```bash
cd web
npm ci
npm run dev        # http://localhost:5173
npx vite build --base=/aiobot/ && npx vite preview --base=/aiobot/
```

- **3D** : `src/graph/scene.js`, chargée à la demande après le premier rendu. La scène rejoue une vraie mission du
  moteur (`public/data/missions/operateur-aiobot.json`) : bras, posture imaginée par le world model, plan retenu,
  trajectoires élaguées, anneau χ², jauge du gate, sphères du SCG et registre XAI. Repli SVG si WebGL est absent,
  l'appareil modeste ou le contexte perdu ; pixel ratio adaptatif (cible 60 FPS) ; `dispose()` à la sortie de page.
- **Démo** : `src/demo.js` rejoue en 2D les 8 missions exportées. Dans l'image Docker (`VITE_ENGINE_API=1`), elle
  peut aussi jouer une mission en direct via l'API du moteur.
- **Données** : `public/data/` est produit par le moteur, rien n'y est retouché :
  `cd engine && python -m aiobot export-demo`.
- **Consentement** : `src/consent.js` (« Tout accepter » et « Tout refuser » au même niveau, aucun script optionnel
  avant le choix, choix gardé 6 mois). Mesure d'audience : renseigner `aiobot:analytics-src` dans `index.html` et
  autoriser son domaine dans la CSP (`vite.config.js` et `nginx/security-headers.conf`).
- **CSP** : posée en balise meta au build (GitHub Pages ne permet pas d'en-têtes HTTP) et en en-tête HTTP par nginx ;
  aucun script, style ou connexion hors de l'origine.
- **Contact** : renseigner `aiobot:contact-email` (ouverture de la messagerie) ou `aiobot:contact-endpoint`
  (API de même origine) dans `index.html`.
- **Avant mise en ligne commerciale** : compléter les éléments surlignés des pages légales (éditeur, contact).
