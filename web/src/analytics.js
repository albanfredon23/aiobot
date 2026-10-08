/**
 * Mesure d'audience éthique : aucun script n'est chargé tant que
 * l'utilisateur n'a pas accepté la catégorie « Mesure d'audience ».
 *
 * Configuration dans le <head> :
 *   <meta name="aiobot:analytics-src" content="https://plausible.io/js/script.js">
 *   <meta name="aiobot:analytics-domain" content="albanfredon23.github.io">
 * Laisser le contenu vide pour ne rien charger (valeur par défaut).
 * Pensez à autoriser le domaine choisi dans la Content-Security-Policy (balise meta Content-Security-Policy de chaque page).
 */
import { hasConsent, onConsentChange } from './consent.js';

let loaded = false;

function meta(name) {
  return document.querySelector(`meta[name="${name}"]`)?.content?.trim() || '';
}

function load() {
  const src = meta('aiobot:analytics-src');
  if (loaded || !src || !hasConsent('analytics')) return;
  const script = document.createElement('script');
  script.defer = true;
  script.src = src;
  script.dataset.domain = meta('aiobot:analytics-domain');
  document.head.append(script);
  loaded = true;
}

export function initAnalytics() {
  load();
  onConsentChange(() => {
    if (!hasConsent('analytics') && loaded) {
      // Retrait du consentement : on recharge pour décharger le script déjà actif.
      window.location.reload();
      return;
    }
    load();
  });
}

/** Événement personnalisé (ex. clic CTA), envoyé uniquement si l'outil est chargé. */
export function track(eventName, props) {
  if (!loaded || typeof window.plausible !== 'function') return;
  window.plausible(eventName, props ? { props } : undefined);
}
