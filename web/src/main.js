import './styles.css';
import { gsap } from 'gsap';
import { initAnalytics, track } from './analytics.js';
import { initConsent } from './consent.js';
import { initDemo } from './demo.js';
import { initContactForm } from './form.js';
import { initNav } from './nav.js';

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

initConsent();
initAnalytics();
initNav();
initDemo({ reducedMotion });
initContactForm();
initReveals();
initGraph();

document.querySelectorAll('a.btn-primary[href="#contact"]').forEach((a) =>
  a.addEventListener('click', () => track('cta_demo', { emplacement: a.closest('section, header')?.id || 'entete' })),
);

/* ------------------------------------------------------------ apparitions */
function initReveals() {
  const items = document.querySelectorAll('.reveal');
  const counters = document.querySelectorAll('[data-count]');
  if (reducedMotion || !('IntersectionObserver' in window)) return;
  document.documentElement.classList.add('js-reveal');
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        io.unobserve(entry.target);
        gsap.fromTo(entry.target, { autoAlpha: 0, y: 24 }, { autoAlpha: 1, y: 0, duration: 0.7, ease: 'power3.out' });
        entry.target.querySelectorAll('[data-count]').forEach(countUp);
      });
    },
    { rootMargin: '0px 0px -10% 0px' },
  );
  items.forEach((el) => io.observe(el));
  counters.forEach((el) => {
    if (!el.closest('.reveal')) countUp(el);
  });
}

function countUp(el) {
  const target = Number(el.dataset.count);
  const suffix = el.dataset.suffix || '';
  const obj = { v: 0 };
  gsap.to(obj, {
    v: target,
    duration: 1.2,
    ease: 'power2.out',
    onUpdate: () => {
      el.textContent = `${Math.round(obj.v).toLocaleString('fr-FR')}${suffix}`;
    },
  });
}

/* ------------------------------------------------------------ graphe 3D */
function webglAvailable() {
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    if (!gl) return false;
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    return true;
  } catch {
    return false;
  }
}

function lowEndDevice() {
  const memory = navigator.deviceMemory;
  const cores = navigator.hardwareConcurrency;
  const saveData = navigator.connection?.saveData;
  return Boolean(saveData || (memory && memory <= 2) || (cores && cores <= 2));
}

function initGraph() {
  const layer = document.querySelector('.graph-layer');
  const canvas = document.getElementById('graph-canvas');
  if (!layer || !canvas) return;
  const tooltip = layer.querySelector('.graph-tooltip');
  const stageSections = [...document.querySelectorAll('.stage-section[data-stage]')];
  const zone = ['#accueil', '#principe', '#fonctionnement'].map((s) => document.querySelector(s)).filter(Boolean);

  let graph = null;
  let stage = 0;
  let inZone = true;
  const visibleZone = new Set(zone);

  const setStage = (value) => {
    stage = value;
    layer.dataset.stage = String(value);
    graph?.setStage(value);
  };
  const refreshActive = () => {
    layer.classList.toggle('is-dimmed', !inZone);
    graph?.setActive(inZone && document.visibilityState === 'visible');
  };

  // Étape courante : la section qui traverse le milieu de l'écran.
  const stageObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) setStage(Number(entry.target.dataset.stage));
      });
    },
    { rootMargin: '-50% 0px -50% 0px' },
  );
  stageSections.forEach((el) => stageObserver.observe(el));

  // Le rendu n'a lieu que lorsque les sections narratives sont à l'écran.
  const zoneObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => (entry.isIntersecting ? visibleZone.add(entry.target) : visibleZone.delete(entry.target)));
      inZone = visibleZone.size > 0;
      refreshActive();
    },
    { rootMargin: '-25% 0px -25% 0px' },
  );
  zone.forEach((el) => zoneObserver.observe(el));
  document.addEventListener('visibilitychange', refreshActive);

  const useFallback = () => {
    graph?.dispose();
    graph = null;
    layer.classList.remove('has-webgl');
  };

  const start = async () => {
    if (graph || !webglAvailable() || lowEndDevice()) return;
    try {
      const { createGraph } = await import('./graph/scene.js');
      graph = createGraph({
        canvas,
        tooltip,
        reducedMotion,
        onContextLost: useFallback,
        onNodeActivate: (node) => {
          document.querySelector(node.target)?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'center' });
        },
      });
      graph.setStage(stage);
      layer.classList.add('has-webgl');
      refreshActive();
    } catch (error) {
      console.warn('Graphe 3D indisponible, affichage 2D.', error);
      useFallback();
    }
  };

  // Chargement différé : la 3D (Three.js) n'est téléchargée qu'après le premier rendu.
  const idle = window.requestIdleCallback || ((cb) => setTimeout(cb, 200));
  idle(() => start(), { timeout: 1500 });

  // Libération explicite de la mémoire GPU à la sortie de page ; reprise depuis le cache.
  window.addEventListener('pagehide', useFallback);
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) start();
  });
}
