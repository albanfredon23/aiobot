/**
 * Gestion du consentement conforme aux recommandations CNIL :
 * - « Tout accepter » et « Tout refuser » au même niveau, même style ;
 * - aucun traceur optionnel avant un choix explicite (blocage préalable) ;
 * - choix modifiable à tout moment (lien « Gérer mes cookies ») ;
 * - choix conservé 6 mois, puis redemandé.
 *
 * Les scripts optionnels sont déclarés en HTML avec
 * <script type="text/plain" data-consent="analytics" data-src="…"> et ne sont
 * activés qu'après consentement à leur catégorie.
 */

const STORAGE_KEY = 'aiobot-consent';
const VERSION = 1;
const MAX_AGE_MS = 1000 * 60 * 60 * 24 * 182; // ~6 mois

export const CATEGORIES = [
  {
    id: 'analytics',
    label: "Mesure d'audience",
    description:
      "Statistiques de fréquentation anonymisées (outil respectueux de la vie privée, sans publicité ni revente). Désactivée par défaut.",
  },
];

const listeners = new Set();

function safeRead() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (data.v !== VERSION || Date.now() - data.date > MAX_AGE_MS) return null;
    return data;
  } catch {
    return null;
  }
}

function safeWrite(choices) {
  const record = { v: VERSION, date: Date.now(), choices };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(record));
  } catch {
    /* stockage indisponible : le choix vaut pour la session en cours */
  }
  return record;
}

let current = safeRead();

export function getConsent() {
  return current ? { ...current.choices } : null;
}

export function hasConsent(category) {
  return Boolean(current && current.choices[category]);
}

export function onConsentChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function apply(choices) {
  current = safeWrite(choices);
  activateScripts();
  listeners.forEach((fn) => fn(getConsent()));
}

function activateScripts() {
  document.querySelectorAll('script[type="text/plain"][data-consent]').forEach((placeholder) => {
    if (!hasConsent(placeholder.dataset.consent)) return;
    const script = document.createElement('script');
    if (placeholder.dataset.src) script.src = placeholder.dataset.src;
    else script.textContent = placeholder.textContent;
    script.defer = true;
    placeholder.replaceWith(script);
  });
}

const allChoices = (value) => Object.fromEntries(CATEGORIES.map((c) => [c.id, value]));

/* ------------------------------------------------------------------ interface */

let banner;
let lastFocus;

function buildBanner() {
  const el = document.createElement('section');
  el.className = 'consent';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'false');
  el.setAttribute('aria-labelledby', 'consent-title');
  el.setAttribute('aria-describedby', 'consent-desc');
  el.innerHTML = `
    <div class="consent-inner">
      <h2 id="consent-title" class="consent-title">Votre vie privée</h2>
      <p id="consent-desc">
        Ce site n'utilise aucun cookie publicitaire. Avec votre accord, nous mesurons l'audience de façon anonymisée
        pour améliorer le site. Vous pouvez accepter, refuser ou personnaliser, et changer d'avis à tout moment via
        « Gérer mes cookies ». <a href="cookies.html">En savoir plus</a>
      </p>
      <form class="consent-prefs" hidden>
        <fieldset>
          <legend>Catégories de traceurs</legend>
          <div class="consent-row">
            <input type="checkbox" id="consent-necessary" checked disabled />
            <label for="consent-necessary"><strong>Strictement nécessaires</strong> : mémorisation de votre choix. Toujours actifs.</label>
          </div>
          ${CATEGORIES.map(
            (c) => `
          <div class="consent-row">
            <input type="checkbox" id="consent-${c.id}" name="${c.id}" />
            <label for="consent-${c.id}"><strong>${c.label}</strong> : ${c.description}</label>
          </div>`,
          ).join('')}
        </fieldset>
        <button type="submit" class="btn btn-ghost btn-small">Enregistrer mes choix</button>
      </form>
      <div class="consent-actions">
        <button type="button" class="btn btn-consent" data-action="refuse">Tout refuser</button>
        <button type="button" class="btn btn-consent" data-action="accept">Tout accepter</button>
        <button type="button" class="link-button" data-action="customize" aria-expanded="false">Personnaliser</button>
      </div>
    </div>`;

  const prefs = el.querySelector('.consent-prefs');
  const customize = el.querySelector('[data-action="customize"]');

  el.addEventListener('click', (event) => {
    const action = event.target.closest('[data-action]')?.dataset.action;
    if (action === 'accept') close(allChoices(true));
    if (action === 'refuse') close(allChoices(false));
    if (action === 'customize') {
      const open = prefs.hidden;
      prefs.hidden = !open;
      customize.setAttribute('aria-expanded', String(open));
      if (open) prefs.querySelector('input:not([disabled])')?.focus();
    }
  });
  prefs.addEventListener('submit', (event) => {
    event.preventDefault();
    const choices = Object.fromEntries(CATEGORIES.map((c) => [c.id, prefs.elements[c.id].checked]));
    close(choices);
  });
  el.addEventListener('keydown', (event) => {
    // Échap = aucun choix enregistré : le bandeau reviendra, rien n'est déposé.
    if (event.key === 'Escape' && current) close(current.choices);
  });
  return el;
}

export function openConsent({ showPreferences = false } = {}) {
  if (!banner) {
    banner = buildBanner();
    document.body.prepend(banner); // premier dans l’ordre de tabulation
  }
  const prefs = banner.querySelector('.consent-prefs');
  CATEGORIES.forEach((c) => {
    prefs.elements[c.id].checked = hasConsent(c.id);
  });
  prefs.hidden = !showPreferences;
  banner.querySelector('[data-action="customize"]').setAttribute('aria-expanded', String(showPreferences));
  banner.hidden = false;
  lastFocus = document.activeElement;
  requestAnimationFrame(() => banner.classList.add('is-visible'));
}

function close(choices) {
  apply(choices);
  banner.classList.remove('is-visible');
  banner.hidden = true;
  if (lastFocus && lastFocus !== document.body && document.contains(lastFocus)) lastFocus.focus();
}

export function initConsent() {
  activateScripts();
  if (!current) openConsent();
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-open-consent]')) {
      event.preventDefault();
      openConsent({ showPreferences: true });
    }
  });
}
