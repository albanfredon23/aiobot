/**
 * Formulaire de contact : case de consentement non pré-cochée, validation
 * accessible, aucune donnée envoyée à un tiers ni à l'outil de mesure d'audience.
 *
 * Le site est statique (GitHub Pages) : par défaut, la demande s'ouvre dans la
 * messagerie du visiteur, adressée à <meta name="aiobot:contact-email">.
 * Si <meta name="aiobot:contact-endpoint"> contient une URL HTTPS de même
 * origine (API, fonction serverless), la demande y est envoyée en JSON.
 */
import { track } from './analytics.js';

const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content?.trim() || '';

const MESSAGES = {
  name: 'Indiquez votre nom.',
  email: 'Indiquez une adresse e-mail valide.',
  consent: 'Cochez la case pour nous autoriser à traiter votre demande.',
};

function setError(input, message) {
  const id = `${input.id}-error`;
  let el = document.getElementById(id);
  if (!message) {
    el?.remove();
    input.removeAttribute('aria-invalid');
    input.setAttribute('aria-describedby', (input.getAttribute('aria-describedby') || '').replace(id, '').trim());
    if (!input.getAttribute('aria-describedby')) input.removeAttribute('aria-describedby');
    return;
  }
  if (!el) {
    el = document.createElement('p');
    el.id = id;
    el.className = 'field-error';
    input.closest('.field, .field-check').append(el);
  }
  el.textContent = message;
  input.setAttribute('aria-invalid', 'true');
  const described = new Set((input.getAttribute('aria-describedby') || '').split(' ').filter(Boolean));
  described.add(id);
  input.setAttribute('aria-describedby', [...described].join(' '));
}

export function initContactForm() {
  const form = document.getElementById('contact-form');
  if (!form) return;
  const status = document.getElementById('cf-status');
  const button = form.querySelector('button[type="submit"]');

  function validate() {
    const { name, email, consent } = form.elements;
    const errors = [];
    const check = (input, ok, key) => {
      setError(input, ok ? '' : MESSAGES[key]);
      if (!ok) errors.push(input);
    };
    check(name, name.value.trim().length > 0, 'name');
    check(email, /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.value.trim()), 'email');
    check(consent, consent.checked, 'consent');
    return errors;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const errors = validate();
    if (errors.length) {
      status.textContent = 'Le formulaire contient des erreurs.';
      errors[0].focus();
      return;
    }
    const f = form.elements;
    const payload = {
      name: f.name.value.trim(),
      email: f.email.value.trim(),
      company: f.company.value.trim(),
      message: f.message.value.trim(),
      lead_magnet: f.lead_magnet.checked,
      consent: f.consent.checked,
    };
    const endpoint = meta('aiobot:contact-endpoint');
    if (!endpoint) {
      const to = meta('aiobot:contact-email');
      if (!to) {
        status.textContent =
          "Le canal de contact n'est pas encore configuré. En attendant, écrivez-nous depuis le dépôt GitHub d'AIOBot (lien en pied de page).";
        return;
      }
      const body = [
        `Nom : ${payload.name}`,
        `E-mail : ${payload.email}`,
        payload.company && `Société : ${payload.company}`,
        payload.lead_magnet && 'Je souhaite recevoir la fiche technique AIOBot.',
        '',
        payload.message,
      ]
        .filter((line) => line !== false && line !== '')
        .join('\n');
      const subject = payload.lead_magnet ? 'AIOBot : demande de démo et fiche technique' : 'AIOBot : demande de démo';
      window.location.href = `mailto:${to}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
      status.textContent = 'Votre messagerie s’ouvre avec la demande préremplie : il ne reste qu’à l’envoyer.';
      track('lead', { fiche_technique: payload.lead_magnet ? 'oui' : 'non', canal: 'email' }); // aucune donnée personnelle
      return;
    }
    button.disabled = true;
    status.textContent = 'Envoi en cours…';
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.status === 429) throw new Error('rate');
      if (!res.ok) throw new Error('http');
      form.reset();
      status.textContent = 'Merci, votre demande a bien été reçue. Nous revenons vers vous rapidement.';
      track('lead', { fiche_technique: payload.lead_magnet ? 'oui' : 'non', canal: 'api' }); // aucune donnée personnelle
    } catch (error) {
      status.textContent =
        error.message === 'rate'
          ? 'Trop de demandes envoyées. Merci de réessayer dans quelques minutes.'
          : "L'envoi a échoué. Vérifiez votre connexion ou réessayez plus tard.";
    } finally {
      button.disabled = false;
    }
  });

  form.addEventListener('change', (event) => {
    if (event.target.getAttribute('aria-invalid') === 'true') validate();
  });
}
