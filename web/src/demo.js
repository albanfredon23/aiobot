/**
 * Démo : rejeu 2D des missions exportées par le moteur (public/data/missions).
 *
 * Chaque image est une vraie décision d'AIOBot : angles mesurés, plan retenu,
 * trajectoires élaguées, filtre χ², budget du gate, rejets du SCG. Quand l'API
 * du moteur est servie sur la même origine (image Docker, VITE_ENGINE_API=1), une
 * mission peut aussi être jouée en direct avec une graine choisie.
 */

const BASE = import.meta.env.BASE_URL;
const VIEW = { x0: -1.0, x1: 1.6, y0: -0.3, y1: 1.12 };

const REASONS = {
  PLAN_ADMISSIBLE: 'Plan admissible et robuste',
  CHI2_FREEZE: 'Gel : le monde ne suit plus le world model',
  INTERVENTION_REQUISE: 'Intervention humaine requise',
  AUCUNE_TRAJECTOIRE_ADMISSIBLE: 'Aucune trajectoire admissible : arrêt sûr',
  ROBUSTESSE_INSUFFISANTE: 'Robustesse insuffisante : arrêt sûr',
  MODELE_INCERTAIN: 'Modèle incertain : arrêt sûr',
};
const REJECTIONS = {
  joint_limit: 'Limites articulaires',
  workspace_floor: 'Plan de travail',
  obstacle_sphere: 'Montage',
  operator_sphere: 'Opérateur',
  speed_limit: 'Vitesse en zone collaborative',
};
const CHI2 = { N: 'normal', A: 'alerte', F: 'gel', I: 'intervention' };
const STATIONS = { prise: 'Prise', passage: 'Passage', depose: 'Dépose', parking: 'Parking' };
const COLORS = {
  grid: 'rgba(148, 163, 184, 0.08)',
  table: '#334155',
  arm: '#f59e0b',
  joint: '#e2e8f0',
  plan: '#2dd4bf',
  pruned: 'rgba(255, 90, 90, 0.75)',
  fixture: '#64748b',
  danger: 'rgba(255, 90, 90, 0.85)',
  collab: 'rgba(251, 191, 36, 0.7)',
  operator: 'rgba(255, 90, 90, 0.16)',
  text: '#cbd5e1',
  ok: '#34d399',
  alert: '#fbbf24',
  freeze: '#ff6b6b',
};

const fmt = (value, digits = 0) =>
  Number(value).toLocaleString('fr-FR', { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** Convertit une image complète de l'API au format compact des fichiers exportés. */
function compactFrame(f) {
  const out = {
    q: f.q, t: f.target, st: f.station, d: f.decision[0], r: f.reason,
    op: f.operator ? f.operator.center : null, kg: f.payload_kg, fl: f.flops,
  };
  if (f.chi2) out.c2 = [f.chi2.d2, f.chi2.status[0]];
  if (f.budget) out.b = [f.budget.n_paths, f.budget.horizon];
  if (f.scg) out.sc = [f.scg.admissible, f.scg.n_trajectories, ...Object.keys(REJECTIONS).map((k) => f.scg.rejections[k])];
  if (f.robustness != null) out.rb = f.robustness;
  if (f.plan) out.p = f.plan;
  if (f.pruned) out.x = f.pruned.slice(0, 3);
  return out;
}

export function initDemo({ reducedMotion }) {
  const form = document.getElementById('demo-form');
  const canvas = document.getElementById('demo-canvas');
  if (!form || !canvas) return;
  const ctx = canvas.getContext('2d');
  const select = document.getElementById('demo-mission');
  const playBtn = document.getElementById('demo-play');
  const speedSel = document.getElementById('demo-speed');
  const timeline = document.getElementById('demo-time');
  const timeOut = document.getElementById('demo-time-out');
  const status = document.getElementById('demo-status');
  const ledgerOut = document.getElementById('demo-ledger');
  const liveBox = document.getElementById('demo-live');
  const seedInput = document.getElementById('demo-seed');
  const liveBtn = document.getElementById('demo-live-run');
  const kpi = (name) => document.querySelector(`[data-kpi="${name}"]`);
  const rejList = document.getElementById('demo-rejections');

  let index = null;
  let mission = null;
  let playhead = 0;
  let playing = false;
  let visible = true;
  let raf = 0;
  let last = 0;
  let shown = -1;
  const cache = new Map();

  /* ---------------------------------------------------------- dessin */
  let W = 0;
  let H = 0;
  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = canvas.getBoundingClientRect();
    W = rect.width;
    H = rect.height;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  const sx = () => W / (VIEW.x1 - VIEW.x0);
  const sy = () => H / (VIEW.y1 - VIEW.y0);
  const px = (x) => (x - VIEW.x0) * sx();
  const py = (y) => H - (y - VIEW.y0) * sy();

  function circle(x, y, r, fill, stroke, dash = []) {
    ctx.beginPath();
    ctx.arc(px(x), py(y), r * sx(), 0, Math.PI * 2);
    if (fill) {
      ctx.fillStyle = fill;
      ctx.fill();
    }
    if (stroke) {
      ctx.setLineDash(dash);
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  function path(points, color, width) {
    if (!points || points.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(px(points[0][0]), py(points[0][1]));
    points.slice(1).forEach((p) => ctx.lineTo(px(p[0]), py(p[1])));
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
  }

  function label(text, x, y, color = COLORS.text, align = 'center') {
    ctx.font = '600 12px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
    ctx.fillStyle = color;
    ctx.textAlign = align;
    ctx.fillText(text, px(x), py(y));
  }

  function currentFrame() {
    if (!mission) return null;
    const i = Math.max(0, Math.min(Math.floor(playhead / index.dt), mission.frames.length - 1));
    return { i, f: mission.frames[i], next: mission.frames[Math.min(i + 1, mission.frames.length - 1)], k: playhead / index.dt - i };
  }

  function draw() {
    if (!W || !index) return;
    ctx.clearRect(0, 0, W, H);
    // Grille de 10 cm.
    ctx.strokeStyle = COLORS.grid;
    ctx.lineWidth = 1;
    for (let x = Math.ceil(VIEW.x0 * 10) / 10; x <= VIEW.x1; x += 0.1) {
      ctx.beginPath();
      ctx.moveTo(px(x), 0);
      ctx.lineTo(px(x), H);
      ctx.stroke();
    }
    for (let y = Math.ceil(VIEW.y0 * 10) / 10; y <= VIEW.y1; y += 0.1) {
      ctx.beginPath();
      ctx.moveTo(0, py(y));
      ctx.lineTo(W, py(y));
      ctx.stroke();
    }
    // Plan de travail.
    const floor = index.envelope.floor_y;
    ctx.fillStyle = 'rgba(51, 65, 85, 0.45)';
    ctx.fillRect(0, py(floor), W, H - py(floor));
    ctx.strokeStyle = COLORS.alert;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(0, py(floor));
    ctx.lineTo(W, py(floor));
    ctx.stroke();
    label('Plan de travail', VIEW.x0 + 0.02, floor - 0.08, COLORS.text, 'left');

    const cur = currentFrame();
    const f = cur?.f;
    // Postes.
    Object.entries(index.stations).forEach(([id, [x, y]]) => {
      const active = f && f.st === id;
      circle(x, y, 0.035, null, active ? COLORS.plan : 'rgba(226, 232, 240, 0.5)', active ? [] : [3, 3]);
      label(STATIONS[id] || id, x, y + (y < 0.3 ? -0.1 : 0.07), active ? COLORS.plan : COLORS.text);
    });
    // Montage : volume interdit.
    const fx = index.fixture;
    circle(fx.center[0], fx.center[1], fx.radius, COLORS.fixture, COLORS.danger, [4, 3]);
    label('Montage', fx.center[0], fx.center[1] + fx.radius + 0.04);
    // Opérateur et zone collaborative.
    if (f && f.op) {
      const r = index.operator.radius;
      circle(f.op[0], f.op[1], r + index.envelope.collaborative_margin, null, COLORS.collab, [6, 4]);
      circle(f.op[0], f.op[1], r, COLORS.operator, COLORS.danger);
      label('Opérateur', f.op[0], f.op[1] + 0.02);
    }
    if (!f) return;
    // Trajectoires élaguées puis plan retenu.
    (f.x || []).forEach((p) => path(p, COLORS.pruned, 1.5));
    path(f.p, COLORS.plan, 2.5);
    // Bras (angles interpolés entre deux décisions).
    const k = Math.min(Math.max(cur.k, 0), 1);
    const q1 = f.q[0] + (cur.next.q[0] - f.q[0]) * k;
    const q2 = f.q[1] + (cur.next.q[1] - f.q[1]) * k;
    const { l1, l2 } = index.arm;
    const e = [l1 * Math.cos(q1), l1 * Math.sin(q1)];
    const t = [e[0] + l2 * Math.cos(q1 + q2), e[1] + l2 * Math.sin(q1 + q2)];
    ctx.lineCap = 'round';
    path([[0, 0], e, t], COLORS.arm, 9);
    ctx.lineCap = 'butt';
    ctx.fillStyle = '#1f2933';
    ctx.fillRect(px(-0.08), py(0), 0.16 * sx(), py(floor) - py(0));
    [[0, 0], e].forEach(([x, y]) => circle(x, y, 0.025, COLORS.joint));
    // Anneau χ² autour de l'outil.
    const c2 = f.c2 ? f.c2[1] : 'N';
    const ring = c2 === 'N' ? COLORS.ok : c2 === 'A' ? COLORS.alert : COLORS.freeze;
    circle(t[0], t[1], 0.045 + Math.min((f.c2?.[0] || 0) / 15, 3) * 0.012, null, ring);
    circle(t[0], t[1], 0.018, ring);
    // Décision.
    ctx.font = '700 14px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillStyle = f.d === 'E' ? COLORS.ok : COLORS.alert;
    ctx.fillText(f.d === 'E' ? 'EXECUTE' : 'HOLD · arrêt sûr', W - 12, 22);
  }

  /* -------------------------------------------------------- panneaux */
  function setText(el, text) {
    if (el && el.textContent !== text) el.textContent = text;
  }

  function updatePanels(force = false) {
    const cur = currentFrame();
    if (!cur) return;
    const { i, f } = cur;
    if (i === shown && !force) return;
    shown = i;
    timeline.value = String(i);
    setText(timeOut, `${i + 1} / ${mission.frames.length} · ${fmt((i + 1) * index.dt, 2)} s`);
    setText(kpi('decision'), f.d === 'E' ? 'EXECUTE' : 'HOLD');
    kpi('decision').dataset.state = f.d;
    setText(kpi('reason'), REASONS[f.r] || f.r);
    setText(kpi('chi2'), f.c2 ? `${fmt(f.c2[0], 1)} (${CHI2[f.c2[1]]})` : '–');
    setText(kpi('budget'), f.b ? `${f.b[0]} trajectoires × ${f.b[1]} pas` : 'aucun calcul (arrêt)');
    setText(kpi('admissible'), f.sc ? `${f.sc[0]} sur ${f.sc[1]}` : '–');
    setText(kpi('robustness'), f.rb != null ? `${fmt(f.rb * 100)} %` : '–');
    setText(kpi('flops'), `${fmt(f.fl / 1e6, 1)} MFLOP`);
    const keys = Object.keys(REJECTIONS);
    const total = f.sc ? Math.max(1, f.sc[1]) : 1;
    rejList.querySelectorAll('li').forEach((li, j) => {
      const n = f.sc ? f.sc[2 + j] : 0;
      li.querySelector('.bar-fill').style.setProperty('--w', `${Math.round((n / total) * 100)}%`);
      setText(li.querySelector('.bar-value'), String(n));
      li.dataset.key = keys[j];
    });
    canvas.setAttribute(
      'aria-label',
      `Pas ${i + 1} sur ${mission.frames.length} : ${f.d === 'E' ? 'EXECUTE' : 'HOLD'}, ${REASONS[f.r] || f.r}, poste visé ${STATIONS[f.st] || f.st}.`,
    );
  }

  function showSummary(data) {
    const s = data.summary;
    setText(kpi('completed'), s.completed ? 'oui, 4 postes sur 4' : s.intervention ? 'non : intervention demandée' : `non : ${s.stations_reached} poste(s) sur 4`);
    setText(kpi('steps'), `${s.steps} (${fmt(s.steps * index.dt, 1)} s)`);
    setText(kpi('violations'), String(s.violations));
    kpi('violations').dataset.state = s.violations ? 'ko' : 'ok';
    setText(kpi('holds'), `${s.holds}, dont ${s.freezes} gel(s) χ²`);
    setText(kpi('compute'), `${fmt(s.flops_total / 1e9, 2)} GFLOP`);
    setText(kpi('energy'), `${fmt(s.mechanical_energy_j, 1)} J`);
    setText(kpi('clearance'), s.min_operator_clearance_m == null ? 'pas d\'opérateur' : `${fmt(s.min_operator_clearance_m * 100)} cm`);
    const ledger = data.ledger;
    ledgerOut.textContent = JSON.stringify(
      { enregistrements: ledger.records, empreinte_de_tete: ledger.head_hash, chaine_verifiee: ledger.verified, extrait: ledger.sample },
      null,
      2,
    );
  }

  /* --------------------------------------------------------- lecture */
  function tick(now) {
    raf = 0;
    if (!playing || !visible || !mission) return;
    // L'horodatage de requestAnimationFrame peut précéder performance.now() : jamais de pas négatif.
    const dt = Math.min(Math.max((now - last) / 1000, 0), 0.1);
    last = now;
    playhead += dt * Number(speedSel.value);
    const end = mission.frames.length * index.dt;
    if (playhead >= end) {
      playhead = end - index.dt / 2;
      setPlaying(false);
      announce(mission.summary.completed ? 'Mission terminée.' : 'Mission arrêtée : voir le bilan.');
    }
    draw();
    updatePanels();
    if (playing) raf = requestAnimationFrame(tick);
  }

  function setPlaying(value) {
    playing = value;
    playBtn.setAttribute('aria-pressed', String(value));
    playBtn.textContent = value ? 'Pause' : 'Lecture';
    if (value && !raf && visible) {
      if (playhead >= mission.frames.length * index.dt - index.dt) playhead = 0;
      last = performance.now();
      raf = requestAnimationFrame(tick);
    }
  }

  function announce(text) {
    setText(status, text);
  }

  function load(data) {
    mission = data;
    playhead = 0;
    shown = -1;
    timeline.max = String(data.frames.length - 1);
    timeline.value = '0';
    showSummary(data);
    draw();
    updatePanels(true);
  }

  async function loadMission(id) {
    try {
      announce('Chargement de la mission…');
      let data = cache.get(id);
      if (!data) {
        const response = await fetch(`${BASE}data/missions/${id}.json`);
        if (!response.ok) throw new Error(String(response.status));
        data = await response.json();
        cache.set(id, data);
      }
      load(data);
      const meta = index.missions.find((m) => m.id === id);
      announce(`${index.scenarios[meta.scenario]}, ${index.arms[meta.arm]} : ${data.frames.length} décisions.`);
      if (!reducedMotion) setPlaying(true);
    } catch {
      announce('La mission n\'a pas pu être chargée.');
    }
  }

  /* -------------------------------------------------- moteur en direct */
  async function detectApi() {
    try {
      const response = await fetch(`${BASE}api/health`, { headers: { accept: 'application/json' } });
      const body = response.ok ? await response.json() : null;
      if (body && body.status === 'ok') liveBox.hidden = false;
    } catch {
      /* site statique : pas d'API, la démo rejoue les missions exportées */
    }
  }

  async function runLive() {
    const meta = index.missions.find((m) => m.id === select.value);
    const seed = Math.max(0, Math.min(100000, Number(seedInput.value) || 0));
    liveBtn.disabled = true;
    announce(`Mission jouée par le moteur (graine ${seed})…`);
    try {
      const response = await fetch(`${BASE}api/mission`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ scenario: meta.scenario, arm: meta.arm, seed }),
      });
      if (!response.ok) throw new Error(String(response.status));
      const body = await response.json();
      load({ ...body, frames: body.frames.map(compactFrame) });
      announce(`Mission jouée en direct par le moteur, graine ${seed} : ${body.frames.length} décisions.`);
      if (!reducedMotion) setPlaying(true);
    } catch (error) {
      announce(error.message === '429' ? 'Trop de demandes, réessayez dans une minute.' : 'Le moteur n\'a pas répondu.');
    } finally {
      liveBtn.disabled = false;
    }
  }

  /* ------------------------------------------------------- démarrage */
  form.addEventListener('submit', (event) => event.preventDefault());
  select.addEventListener('change', () => {
    setPlaying(false);
    loadMission(select.value);
  });
  playBtn.addEventListener('click', () => mission && setPlaying(!playing));
  timeline.addEventListener('input', () => {
    if (!mission) return;
    setPlaying(false);
    playhead = (Number(timeline.value) + 0.01) * index.dt;
    draw();
    updatePanels(true);
  });
  liveBtn?.addEventListener('click', runLive);

  new ResizeObserver(resize).observe(canvas);
  // La lecture est suspendue hors écran et onglet masqué.
  new IntersectionObserver((entries) => {
    visible = entries[0].isIntersecting && document.visibilityState === 'visible';
    if (visible && playing && !raf) {
      last = performance.now();
      raf = requestAnimationFrame(tick);
    }
  }).observe(canvas);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') visible = false;
  });

  rejList.replaceChildren(
    ...Object.values(REJECTIONS).map((name) => {
      const li = document.createElement('li');
      li.innerHTML = '<span class="bar-label"></span><span class="bar"><span class="bar-fill"></span></span><span class="bar-value">0</span>';
      li.querySelector('.bar-label').textContent = name;
      return li;
    }),
  );

  fetch(`${BASE}data/index.json`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
    .then((data) => {
      index = data;
      const groups = { aiobot: [], other: [] };
      data.missions.forEach((m) => {
        const option = new Option(
          m.arm === 'aiobot' ? data.scenarios[m.scenario] : `${data.scenarios[m.scenario]} · ${data.arms[m.arm]}`,
          m.id,
        );
        (m.arm === 'aiobot' ? groups.aiobot : groups.other).push(option);
      });
      const g1 = document.createElement('optgroup');
      g1.label = 'AIOBot complet';
      g1.append(...groups.aiobot);
      const g2 = document.createElement('optgroup');
      g2.label = 'Ablations (une brique retirée)';
      g2.append(...groups.other);
      select.replaceChildren(g1, g2);
      select.value = 'operateur-aiobot';
      select.disabled = false;
      playBtn.disabled = false;
      timeline.disabled = false;
      resize();
      loadMission(select.value);
      // Uniquement dans l'image Docker (VITE_ENGINE_API=1), où l'API du moteur est servie sur la même origine.
      if (import.meta.env.VITE_ENGINE_API === '1') detectApi();
    })
    .catch(() => announce('Les données de démonstration n\'ont pas pu être chargées.'));
}
