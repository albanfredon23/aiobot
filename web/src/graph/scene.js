/**
 * Scène 3D AIOBot (Three.js) : une cellule robotisée pilotée par un world model.
 *
 * La scène rejoue une vraie mission exportée par le moteur
 * (public/data/missions/operateur-aiobot.json, `python -m aiobot export-demo`) :
 * angles du bras, plan retenu, trajectoires élaguées par le SCG, filtre χ²,
 * budget du gate et décisions EXECUTE / HOLD. Rien n'y est inventé.
 *
 * - Bras 2 axes (orange) : posé à chaque pas sur les angles mesurés, interpolés à 60 FPS.
 * - Bras fantôme (turquoise) : la posture que le world model imagine au bout du plan.
 * - Plan retenu (turquoise) et trajectoires élaguées (rouge) : coordonnées de l'outil.
 * - Anneau χ² autour de l'outil : vert si le monde suit le modèle, ambre en alerte, rouge au gel.
 * - Jauge du gate : nombre de trajectoires imaginées à ce pas (64 à 256).
 * - Sphères du SCG : montage (volume interdit), opérateur et sa zone collaborative.
 * - Registre XAI : un bloc par décision, vert pour EXECUTE, ambre pour HOLD.
 *
 * Performance : pixel ratio adaptatif (cible 60 FPS), rendu suspendu hors
 * écran ou onglet masqué, aucune géométrie recréée pendant l'animation.
 * Mémoire : dispose() libère géométries, matériaux, textures, environnement
 * et contexte WebGL.
 */
import * as THREE from 'three';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';

const S = 3.2; // 1 m de la cellule = 3,2 unités de scène
const MISSION_URL = `${import.meta.env.BASE_URL}data/missions/operateur-aiobot.json`;
const INDEX_URL = `${import.meta.env.BASE_URL}data/index.json`;

const C = {
  bg: 0x070b10,
  arm: 0xf59e0b,
  joint: 0x1f2933,
  ghost: 0x2dd4bf,
  plan: 0x2dd4bf,
  pruned: 0xff5a5a,
  ok: 0x34d399,
  alert: 0xfbbf24,
  freeze: 0xff5a5a,
  operator: 0x93c5fd,
  danger: 0xff5a5a,
  collab: 0xfbbf24,
  fixture: 0x94a3b8,
  table: 0x18222c,
  station: 0xe2e8f0,
  gateOff: 0x1e293b,
  gateOn: 0xa78bfa,
};

// Valeurs par défaut (écrasées par data/index.json) : géométrie de la cellule du moteur.
const CELL = {
  dt: 0.05,
  l1: 0.55,
  l2: 0.45,
  floorY: -0.12,
  collabMargin: 0.35,
  fixture: { center: [0.05, 0.42], radius: 0.1 },
  operator: { center: [0.98, 0.42], radius: 0.22 },
  stations: { prise: [0.72, 0.02], passage: [0.02, 0.8], depose: [-0.62, 0.1], parking: [0.3, 0.72] },
};
const STATION_LABELS = { prise: 'Prise', passage: 'Passage', depose: 'Dépose', parking: 'Parking' };
const GROUND_Y = -0.85;
const MAX_PATHS = 256;

export const NODES = {
  arm: { label: 'Bras 2 axes', desc: "Posé à chaque pas sur les angles réels de la mission rejouée.", target: '#etape-world-model' },
  ghost: { label: 'World model', desc: "La posture que le modèle appris imagine au bout du plan, avant d'agir.", target: '#etape-world-model' },
  halo: { label: 'Filtre χ²', desc: 'Écart entre la mesure et la prédiction : vert, ambre, rouge au gel.', target: '#etape-chi2' },
  gate: { label: 'Gate', desc: 'Nombre de trajectoires imaginées à ce pas, de 64 à 256.', target: '#etape-gate' },
  plan: { label: 'TAP', desc: "Plan retenu (turquoise) et trajectoires élaguées (rouge).", target: '#etape-tap' },
  operator: { label: 'Opérateur', desc: 'Sphère protégée et zone collaborative où la vitesse est réduite.', target: '#etape-scg' },
  fixture: { label: 'Montage', desc: 'Volume interdit : toute trajectoire qui le traverse est élaguée.', target: '#etape-scg' },
  ledger: { label: 'XAI Ledger', desc: 'Un bloc chaîné SHA-256 par décision : vert EXECUTE, ambre HOLD.', target: '#etape-decision' },
};

// Vues caméra par étape, en mètres : [position, cible]
const VIEWS = [
  [[0.1, 0.75, 3.7], [0.12, 0.3, 0]],
  [[0.6, 0.75, 2.2], [0.3, 0.35, 0]],
  [[0.85, 0.55, 1.8], [0.45, 0.3, 0]],
  [[0.45, 0.75, 2.1], [-0.2, 0.4, -0.4]],
  [[0.35, 1.05, 2.0], [0.28, 0.38, 0]],
  [[1.25, 0.75, 2.5], [0.55, 0.32, 0]],
  [[-0.1, 1.05, 3.1], [0.05, 0.6, 0]],
];

// Mise en avant des éléments selon l'étape (0 = discret, 1 = au premier plan).
const FOCUS = {
  ghost: [0.35, 1, 0.3, 0.3, 0.5, 0.3, 0.3],
  halo: [0.55, 0.4, 1, 0.4, 0.4, 0.5, 0.5],
  gate: [0.45, 0.3, 0.3, 1, 0.5, 0.3, 0.3],
  plan: [0.7, 0.6, 0.5, 0.6, 1, 0.8, 0.6],
  scg: [0.5, 0.3, 0.3, 0.3, 0.6, 1, 0.5],
  ledger: [0.45, 0.2, 0.2, 0.2, 0.2, 0.3, 1],
};

const v3 = (x, y, z = 0) => new THREE.Vector3(x * S, y * S, z * S);

function makeLabelTexture(text) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const font = '600 44px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  ctx.font = font;
  const width = Math.ceil(ctx.measureText(text).width) + 56;
  canvas.width = width;
  canvas.height = 84;
  ctx.font = font;
  ctx.fillStyle = 'rgba(7, 11, 16, 0.82)';
  ctx.beginPath();
  ctx.roundRect(0, 0, width, 84, 24);
  ctx.fill();
  ctx.fillStyle = '#eef4f2';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 28, 44);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return { texture, aspect: width / 84 };
}

/** Cinématique inverse du bras 2 axes, sur la branche de coude demandée. */
function inverseKinematics(x, y, elbowSign) {
  const { l1, l2 } = CELL;
  const r2 = x * x + y * y;
  const c2 = THREE.MathUtils.clamp((r2 - l1 * l1 - l2 * l2) / (2 * l1 * l2), -1, 1);
  const q2 = elbowSign * Math.acos(c2);
  const q1 = Math.atan2(y, x) - Math.atan2(l2 * Math.sin(q2), l1 + l2 * Math.cos(q2));
  return [q1, q2];
}

export function createGraph({ canvas, tooltip, reducedMotion = false, onContextLost, onNodeActivate }) {
  const isSmall = window.matchMedia('(max-width: 720px)').matches;
  const maxDpr = Math.min(window.devicePixelRatio || 1, 2);
  let dpr = isSmall ? Math.min(maxDpr, 1.5) : maxDpr;
  const hud = canvas.parentElement?.querySelector('.scene-hud') ?? null;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(dpr);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.setClearColor(C.bg, 0);

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(C.bg, 14, 30);

  // Éclairage réaliste : environnement studio pré-filtré (PBR) + lumières d'atelier.
  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  const envTexture = pmrem.fromScene(room, 0.04).texture;
  scene.environment = envTexture;
  room.traverse((o) => {
    o.geometry?.dispose();
    o.material?.dispose?.();
  });
  pmrem.dispose();
  scene.add(new THREE.HemisphereLight(0xd6e6f0, 0x0a1016, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 1.3);
  key.position.set(3, 8, 6);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x5eead4, 0.5);
  rim.position.set(-6, 3, -5);
  scene.add(rim);

  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 60);
  const camPos = v3(...VIEWS[0][0]);
  const camLook = v3(...VIEWS[0][1]);
  camera.position.copy(camPos);

  const root = new THREE.Group();
  scene.add(root);
  const pickables = [];
  const pickable = (mesh, node) => {
    mesh.userData.node = node;
    pickables.push(mesh);
    return mesh;
  };
  const labels = [];
  const addLabel = (text, pos, height = 0.3) => {
    const { texture, aspect } = makeLabelTexture(text);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, opacity: 0.9 }));
    const h = isSmall ? height * 1.25 : height;
    sprite.scale.set(h * aspect, h, 1);
    sprite.position.copy(pos);
    sprite.renderOrder = 10;
    root.add(sprite);
    labels.push(sprite);
    return sprite;
  };

  /* --------------------------------------------------------- l'atelier */
  const ground = new THREE.GridHelper(40, 64, 0x1d3a44, 0x0f1d24);
  ground.position.y = GROUND_Y * S;
  ground.material.transparent = true;
  ground.material.opacity = 0.55;
  root.add(ground);

  const steel = new THREE.MeshStandardMaterial({ color: C.table, metalness: 0.6, roughness: 0.45 });
  const tableTop = new THREE.Mesh(new THREE.BoxGeometry(2.6 * S, 0.05 * S, 1.1 * S), steel);
  tableTop.position.set(0.2 * S, (CELL.floorY - 0.025) * S, -0.1 * S);
  root.add(tableTop);
  const legGeo = new THREE.BoxGeometry(0.06 * S, (CELL.floorY - 0.05 - GROUND_Y) * S, 0.06 * S);
  [[-1.05, 0.38], [1.45, 0.38], [-1.05, -0.58], [1.45, -0.58]].forEach(([x, z]) => {
    const leg = new THREE.Mesh(legGeo, steel);
    leg.position.set(x * S, ((CELL.floorY - 0.05 + GROUND_Y) / 2) * S, z * S);
    root.add(leg);
  });
  // Liseré de sécurité au bord du plan de travail.
  const edgeMat = new THREE.MeshBasicMaterial({ color: C.alert, transparent: true, opacity: 0.55 });
  const edgeStrip = new THREE.Mesh(new THREE.BoxGeometry(2.6 * S, 0.012 * S, 0.02 * S), edgeMat);
  edgeStrip.position.set(0.2 * S, CELL.floorY * S, 0.45 * S);
  root.add(edgeStrip);

  /* ---------------------------------------------------------- le bras */
  const armMat = new THREE.MeshStandardMaterial({ color: C.arm, metalness: 0.35, roughness: 0.38 });
  const jointMat = new THREE.MeshStandardMaterial({ color: C.joint, metalness: 0.75, roughness: 0.3 });
  const partMat = new THREE.MeshStandardMaterial({ color: 0x64748b, metalness: 0.8, roughness: 0.25 });

  function buildArm(material, jointMaterial, withPart) {
    const shoulder = new THREE.Group();
    const link1 = new THREE.Mesh(new THREE.BoxGeometry(CELL.l1 * S, 0.075 * S, 0.1 * S), material);
    link1.position.x = (CELL.l1 * S) / 2;
    shoulder.add(link1);
    const elbow = new THREE.Group();
    elbow.position.x = CELL.l1 * S;
    shoulder.add(elbow);
    const elbowJoint = new THREE.Mesh(new THREE.CylinderGeometry(0.06 * S, 0.06 * S, 0.14 * S, 32), jointMaterial);
    elbowJoint.rotation.x = Math.PI / 2;
    elbow.add(elbowJoint);
    const link2 = new THREE.Mesh(new THREE.BoxGeometry(CELL.l2 * S, 0.06 * S, 0.08 * S), material);
    link2.position.x = (CELL.l2 * S) / 2;
    elbow.add(link2);
    const wrist = new THREE.Group();
    wrist.position.x = CELL.l2 * S;
    elbow.add(wrist);
    const flange = new THREE.Mesh(new THREE.CylinderGeometry(0.045 * S, 0.045 * S, 0.11 * S, 24), jointMaterial);
    flange.rotation.x = Math.PI / 2;
    wrist.add(flange);
    const fingerGeo = new THREE.BoxGeometry(0.07 * S, 0.016 * S, 0.035 * S);
    [-1, 1].forEach((side) => {
      const finger = new THREE.Mesh(fingerGeo, jointMaterial);
      finger.position.set(0.055 * S, side * 0.035 * S, 0);
      wrist.add(finger);
    });
    let part = null;
    if (withPart) {
      part = new THREE.Mesh(new THREE.BoxGeometry(0.05 * S, 0.05 * S, 0.05 * S), partMat);
      part.position.x = 0.065 * S;
      wrist.add(part);
    }
    return { shoulder, elbow, wrist, links: [link1, link2], part };
  }

  const pedestal = new THREE.Mesh(new THREE.CylinderGeometry(0.11 * S, 0.14 * S, -CELL.floorY * S, 40), jointMat);
  pedestal.position.y = (CELL.floorY / 2) * S;
  root.add(pedestal);
  const shoulderJoint = new THREE.Mesh(new THREE.CylinderGeometry(0.08 * S, 0.08 * S, 0.16 * S, 36), jointMat);
  shoulderJoint.rotation.x = Math.PI / 2;
  root.add(shoulderJoint);

  const arm = buildArm(armMat, jointMat, true);
  root.add(arm.shoulder);
  arm.links.forEach((m) => pickable(m, NODES.arm));

  // Fantôme : ce que le world model imagine au bout du plan.
  const ghostMat = new THREE.MeshStandardMaterial({
    color: C.ghost, emissive: C.ghost, emissiveIntensity: 0.6, transparent: true, opacity: 0.22, depthWrite: false, roughness: 0.4,
  });
  const ghost = buildArm(ghostMat, ghostMat, false);
  ghost.shoulder.position.z = 0.002;
  root.add(ghost.shoulder);
  ghost.links.forEach((m) => pickable(m, NODES.ghost));

  /* ------------------------------------------- volumes du SCG et postes */
  const fixtureCore = new THREE.Mesh(
    new THREE.SphereGeometry(CELL.fixture.radius * S, 40, 28),
    new THREE.MeshStandardMaterial({ color: C.fixture, metalness: 0.7, roughness: 0.35 }),
  );
  root.add(pickable(fixtureCore, NODES.fixture));
  const fixtureShellMat = new THREE.MeshBasicMaterial({ color: C.danger, wireframe: true, transparent: true, opacity: 0.22, depthWrite: false });
  const fixtureShell = new THREE.Mesh(new THREE.IcosahedronGeometry((CELL.fixture.radius + 0.02) * S, 2), fixtureShellMat);
  root.add(fixtureShell);
  const fixtureLabel = addLabel('Montage', v3(0, 0, 0), 0.19);

  // Opérateur : silhouette, sphère protégée et zone collaborative.
  const operator = new THREE.Group();
  const humanMat = new THREE.MeshStandardMaterial({ color: 0x475569, metalness: 0.1, roughness: 0.75 });
  const vestMat = new THREE.MeshStandardMaterial({ color: 0xc2410c, emissive: 0x431407, emissiveIntensity: 0.25, roughness: 0.6 });
  const legGeoH = new THREE.CapsuleGeometry(0.055 * S, 0.7 * S, 6, 14);
  const legs = new THREE.Group();
  [-0.07, 0.07].forEach((dx) => {
    const leg = new THREE.Mesh(legGeoH, humanMat);
    leg.position.set(dx * S, (GROUND_Y + 0.42) * S, 0);
    legs.add(leg);
  });
  const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.12 * S, 0.34 * S, 8, 20), vestMat);
  torso.position.y = (GROUND_Y + 1.13) * S;
  torso.scale.z = 0.7;
  const armGeoH = new THREE.CapsuleGeometry(0.04 * S, 0.5 * S, 6, 12);
  const arms = [-1, 1].map((side) => {
    const a = new THREE.Mesh(armGeoH, humanMat);
    a.position.set(side * 0.17 * S, (GROUND_Y + 1.12) * S, 0);
    a.rotation.z = side * 0.12;
    return a;
  });
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.095 * S, 24, 16), humanMat);
  head.position.y = (GROUND_Y + 1.56) * S;
  operator.add(legs, torso, head, ...arms);
  operator.position.z = -0.32 * S;
  root.add(operator);
  [...legs.children, torso, head, ...arms].forEach((m) => pickable(m, NODES.operator));
  const opSphereMat = new THREE.MeshStandardMaterial({
    color: C.danger, emissive: C.danger, emissiveIntensity: 0.4, transparent: true, opacity: 0.14, depthWrite: false, roughness: 0.2,
  });
  const opSphere = new THREE.Mesh(new THREE.SphereGeometry(CELL.operator.radius * S, 40, 28), opSphereMat);
  root.add(pickable(opSphere, NODES.operator));
  const collabMat = new THREE.MeshBasicMaterial({ color: C.collab, wireframe: true, transparent: true, opacity: 0.1, depthWrite: false });
  const collab = new THREE.Mesh(new THREE.IcosahedronGeometry((CELL.operator.radius + CELL.collabMargin) * S, 3), collabMat);
  root.add(collab);
  const operatorLabel = addLabel('Opérateur', v3(0, 0, 0), 0.2);

  const stationMat = new THREE.MeshStandardMaterial({ color: C.station, emissive: C.station, emissiveIntensity: 0.2, transparent: true, opacity: 0.5 });
  const stationGeo = new THREE.TorusGeometry(0.035 * S, 0.006 * S, 10, 40);
  const stations = new Map();
  function placeStations() {
    stations.forEach(({ ring, label }) => {
      ring.material.dispose();
      root.remove(ring, label);
      label.material.map.dispose();
      label.material.dispose();
      labels.splice(labels.indexOf(label), 1);
    });
    stations.clear();
    Object.entries(CELL.stations).forEach(([id, [x, y]]) => {
      const ring = new THREE.Mesh(stationGeo, stationMat.clone());
      ring.position.copy(v3(x, y, 0));
      root.add(ring);
      const label = addLabel(STATION_LABELS[id] || id, v3(x, y + (y < 0.3 ? -0.1 : 0.09), 0.05), 0.17);
      stations.set(id, { ring, label });
    });
  }

  /* -------------------------------------------- plan TAP et élagage SCG */
  const POINTS = 11;
  const PRUNED = 3;
  const lineAttr = (n) => new THREE.BufferAttribute(new Float32Array(n * 3), 3);
  const planGeo = new THREE.BufferGeometry();
  planGeo.setAttribute('position', lineAttr(POINTS));
  const planMat = new THREE.LineBasicMaterial({ color: C.plan, transparent: true, opacity: 0.9 });
  const planLine = new THREE.Line(planGeo, planMat);
  planLine.frustumCulled = false;
  root.add(planLine);
  const prunedGeo = new THREE.BufferGeometry();
  prunedGeo.setAttribute('position', lineAttr(PRUNED * (POINTS - 1) * 2));
  const prunedMat = new THREE.LineBasicMaterial({ color: C.pruned, transparent: true, opacity: 0.6 });
  const prunedLines = new THREE.LineSegments(prunedGeo, prunedMat);
  prunedLines.frustumCulled = false;
  root.add(prunedLines);
  const beadGeo = new THREE.SphereGeometry(0.011 * S, 12, 8);
  const planBeadMat = new THREE.MeshBasicMaterial({ color: C.plan, transparent: true });
  const planBeads = new THREE.InstancedMesh(beadGeo, planBeadMat, POINTS);
  planBeads.frustumCulled = false;
  root.add(pickable(planBeads, NODES.plan));
  const prunedBeadMat = new THREE.MeshBasicMaterial({ color: C.pruned, transparent: true });
  const prunedBeads = new THREE.InstancedMesh(beadGeo, prunedBeadMat, PRUNED * POINTS);
  prunedBeads.frustumCulled = false;
  root.add(pickable(prunedBeads, NODES.plan));
  const PLAN_Z = 0.09;

  /* --------------------------------------------------------- filtre χ² */
  const haloMat = new THREE.MeshBasicMaterial({ color: C.ok, transparent: true, opacity: 0.8, depthWrite: false });
  const halo = new THREE.Mesh(new THREE.TorusGeometry(0.075 * S, 0.006 * S, 12, 64), haloMat);
  root.add(pickable(halo, NODES.halo));
  const haloOuterMat = new THREE.MeshBasicMaterial({ color: C.ok, transparent: true, opacity: 0.25, depthWrite: false, side: THREE.DoubleSide });
  const haloOuter = new THREE.Mesh(new THREE.RingGeometry(0.085 * S, 0.1 * S, 64), haloOuterMat);
  root.add(haloOuter);

  /* ------------------------------------------------------- jauge du gate */
  const GATE_SEG = 16;
  const gateGeo = new THREE.BoxGeometry(0.09 * S, 0.035 * S, 0.05 * S);
  const gateMat = new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xffffff, emissiveIntensity: 0.25, roughness: 0.5 });
  const gateBars = new THREE.InstancedMesh(gateGeo, gateMat, GATE_SEG);
  const m4 = new THREE.Matrix4();
  const gateBase = v3(-0.42, 0.0, -0.7);
  for (let i = 0; i < GATE_SEG; i += 1) {
    m4.makeTranslation(gateBase.x, gateBase.y + i * 0.05 * S, gateBase.z);
    gateBars.setMatrixAt(i, m4);
    gateBars.setColorAt(i, new THREE.Color(C.gateOff));
  }
  root.add(pickable(gateBars, NODES.gate));
  addLabel('Gate', v3(-0.42, 0.86, -0.7), 0.2);

  /* ------------------------------------------------------- registre XAI */
  const CHAIN = isSmall ? 10 : 16;
  const blockGeo = new THREE.BoxGeometry(0.075 * S, 0.075 * S, 0.075 * S);
  const blockMat = new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xffffff, emissiveIntensity: 0.15, metalness: 0.3, roughness: 0.35 });
  const chain = new THREE.InstancedMesh(blockGeo, blockMat, CHAIN);
  const chainStart = v3(-0.85, 1.25, -0.45);
  const chainStep = 0.12 * S;
  const chainColors = Array.from({ length: CHAIN }, () => new THREE.Color(0x1e293b));
  root.add(pickable(chain, NODES.ledger));
  const chainLinkGeo = new THREE.BufferGeometry().setFromPoints([
    chainStart.clone(), chainStart.clone().add(new THREE.Vector3((CHAIN - 1) * chainStep, 0, 0)),
  ]);
  const chainLinkMat = new THREE.LineBasicMaterial({ color: 0x475569, transparent: true, opacity: 0.6 });
  root.add(new THREE.Line(chainLinkGeo, chainLinkMat));
  addLabel('XAI Ledger · SHA-256', chainStart.clone().add(new THREE.Vector3(((CHAIN - 1) * chainStep) / 2, 0.13 * S, 0)), 0.2);

  /* -------------------------------------------------------------- état */
  const state = {
    stage: 0,
    stageTime: 0,
    time: 0,
    running: false,
    disposed: false,
    hovered: null,
    pointer: new THREE.Vector2(),
    parallax: new THREE.Vector2(),
    frames: null,
    playhead: 0,
    lastIndex: -1,
    hudAt: 0,
    flash: 0,
    focus: Object.fromEntries(Object.keys(FOCUS).map((k) => [k, FOCUS[k][0]])),
    ghostQ: [Math.PI / 2, -1.6],
    opX: 1.6,
    opVisible: 0,
  };
  const tmp = new THREE.Vector3();
  const tmpLook = new THREE.Vector3();
  const color = new THREE.Color();

  function placeCellObjects() {
    fixtureCore.position.copy(v3(...CELL.fixture.center));
    fixtureShell.position.copy(fixtureCore.position);
    fixtureLabel.position.copy(v3(CELL.fixture.center[0], CELL.fixture.center[1] + CELL.fixture.radius + 0.07, 0.05));
    placeStations();
  }
  placeCellObjects();

  function poseArm(target, q1, q2) {
    target.shoulder.rotation.z = q1;
    target.elbow.rotation.z = q2;
  }

  function writePath(attr, offset, path) {
    for (let i = 0; i < POINTS; i += 1) {
      const p = path[Math.min(i, path.length - 1)];
      attr.setXYZ(offset + i, p[0] * S, p[1] * S, PLAN_Z * S);
    }
  }

  function applyFrame(frame) {
    // Plan retenu.
    const planPos = planGeo.attributes.position;
    if (frame.p) {
      writePath(planPos, 0, frame.p);
      frame.p.forEach((_, i) => {
        const p = frame.p[Math.min(i, frame.p.length - 1)];
        m4.makeTranslation(p[0] * S, p[1] * S, PLAN_Z * S);
        planBeads.setMatrixAt(i, m4);
      });
      planBeads.count = Math.min(POINTS, frame.p.length);
      planLine.visible = true;
    } else {
      planBeads.count = 0;
      planLine.visible = false;
    }
    planPos.needsUpdate = true;
    planBeads.instanceMatrix.needsUpdate = true;

    // Trajectoires élaguées par le SCG (segments).
    const prunedPos = prunedGeo.attributes.position;
    const paths = frame.x || [];
    let seg = 0;
    let bead = 0;
    paths.slice(0, PRUNED).forEach((path) => {
      for (let i = 0; i < path.length - 1 && i < POINTS - 1; i += 1) {
        prunedPos.setXYZ(seg++, path[i][0] * S, path[i][1] * S, PLAN_Z * S);
        prunedPos.setXYZ(seg++, path[i + 1][0] * S, path[i + 1][1] * S, PLAN_Z * S);
      }
      const end = path[path.length - 1];
      m4.makeTranslation(end[0] * S, end[1] * S, PLAN_Z * S);
      prunedBeads.setMatrixAt(bead++, m4);
    });
    prunedGeo.setDrawRange(0, seg);
    prunedPos.needsUpdate = true;
    prunedBeads.count = bead;
    prunedBeads.instanceMatrix.needsUpdate = true;

    // Jauge du gate : part du budget maximal (256 trajectoires).
    const n = frame.b ? frame.b[0] : 0;
    const lit = Math.ceil((GATE_SEG * n) / MAX_PATHS);
    for (let i = 0; i < GATE_SEG; i += 1) {
      gateBars.setColorAt(i, color.setHex(i < lit ? C.gateOn : C.gateOff));
    }
    gateBars.instanceColor.needsUpdate = true;

    // χ² : couleur selon le statut (N normal, A alerte, F gel, I intervention).
    const status = frame.c2 ? frame.c2[1] : 'N';
    const hex = status === 'N' ? C.ok : status === 'A' ? C.alert : C.freeze;
    haloMat.color.setHex(hex);
    haloOuterMat.color.setHex(hex);
    if (status === 'F' || status === 'I') state.flash = 1;

    // Registre : un bloc par décision, le plus récent à droite.
    chainColors.shift();
    chainColors.push(new THREE.Color(frame.d === 'E' ? C.ok : C.alert));
    chainColors.forEach((c, i) => chain.setColorAt(i, c));
    chain.instanceColor.needsUpdate = true;

    // Poste visé.
    stations.forEach(({ ring }, id) => {
      const active = id === frame.st;
      ring.material.emissiveIntensity = active ? 1.2 : 0.2;
      ring.material.opacity = active ? 1 : 0.45;
      ring.material.color.setHex(active ? C.plan : C.station);
      ring.material.emissive.setHex(active ? C.plan : C.station);
    });

    // Charge utile : arête du cube proportionnelle à la racine cubique de la masse.
    const side = 0.6 + 0.5 * Math.cbrt(Math.max(frame.kg ?? 0.15, 0.05));
    arm.part.scale.setScalar(side);
    updateHud(frame);
  }

  function updateHud(frame) {
    if (!hud) return;
    const set = (key, text) => {
      const el = hud.querySelector(`[data-hud="${key}"]`);
      if (el && el.textContent !== text) el.textContent = text;
    };
    const idx = state.lastIndex + 1;
    set('step', `${idx} / ${state.frames.length}`);
    set('decision', frame.d === 'E' ? 'EXECUTE' : 'HOLD');
    hud.dataset.decision = frame.d;
    set('chi2', frame.c2 ? `${frame.c2[0].toFixed(1)} · ${{ N: 'normal', A: 'alerte', F: 'gel', I: 'intervention' }[frame.c2[1]]}` : '–');
    set('budget', frame.b ? `${frame.b[0]} × ${frame.b[1]} pas` : '–');
    set('scg', frame.sc ? `${frame.sc[0]} / ${frame.sc[1]} admissibles` : '–');
    set('reason', frame.r.replaceAll('_', ' ').toLowerCase());
  }

  /* -------------------------------------------------------- chargement */
  let aborted = false;
  const controller = new AbortController();
  Promise.all([
    fetch(INDEX_URL, { signal: controller.signal }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    fetch(MISSION_URL, { signal: controller.signal }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status)))),
  ])
    .then(([index, mission]) => {
      if (aborted || state.disposed) return;
      if (index) {
        CELL.dt = index.dt;
        CELL.l1 = index.arm.l1;
        CELL.l2 = index.arm.l2;
        CELL.floorY = index.envelope.floor_y;
        CELL.collabMargin = index.envelope.collaborative_margin;
        CELL.fixture = index.fixture;
        CELL.operator = index.operator;
        CELL.stations = index.stations;
        placeCellObjects();
      }
      state.frames = mission.frames;
      // Mouvement réduit : une image fixe parlante (opérateur présent, élagage visible).
      if (reducedMotion) {
        const i = state.frames.findIndex((f) => f.op && f.x && f.x.length >= 2);
        state.playhead = Math.max(0, i) * CELL.dt;
      }
      if (hud) hud.hidden = false;
      if (!state.running) renderOnce();
    })
    .catch(() => {
      // Sans données, la scène reste une cellule statique : le contenu HTML porte l'information.
    });

  /* ---------------------------------------------------------- animation */
  function updateMission(dt, snap = false) {
    const frames = state.frames;
    if (!frames) {
      poseArm(arm, Math.PI / 2 + Math.sin(state.time * 0.6) * 0.2, -1.6);
      poseArm(ghost, Math.PI / 2, -1.6);
      return;
    }
    const total = frames.length * CELL.dt;
    state.playhead += dt;
    if (state.playhead > total + 1.2) {
      state.playhead = 0;
      state.lastIndex = -1;
    }
    const f = Math.min(state.playhead / CELL.dt, frames.length - 1);
    const i = Math.floor(f);
    const a = frames[i];
    const b = frames[Math.min(i + 1, frames.length - 1)];
    const k = f - i;
    const q1 = a.q[0] + (b.q[0] - a.q[0]) * k;
    const q2 = a.q[1] + (b.q[1] - a.q[1]) * k;
    poseArm(arm, q1, q2);
    if (i !== state.lastIndex) {
      // Rattrape les décisions sautées (onglet ralenti) pour que le registre reste fidèle.
      if (i < state.lastIndex) state.lastIndex = -1;
      const from = Math.max(state.lastIndex + 1, i - CHAIN + 1);
      for (let j = from; j <= i; j += 1) {
        state.lastIndex = j;
        applyFrame(frames[j]);
      }
    }

    // Fantôme : posture imaginée au bout du plan (branche de coude du bras réel).
    const end = a.p ? a.p[a.p.length - 1] : null;
    const target = end ? inverseKinematics(end[0], end[1], Math.sign(q2) || 1) : [q1, q2];
    const g = snap ? 1 : 1 - Math.exp(-dt * 10);
    state.ghostQ[0] += (target[0] - state.ghostQ[0]) * g;
    state.ghostQ[1] += (target[1] - state.ghostQ[1]) * g;
    poseArm(ghost, state.ghostQ[0], state.ghostQ[1]);

    // Outil : anneau χ² centré sur l'effecteur.
    const ex = CELL.l1 * Math.cos(q1) + CELL.l2 * Math.cos(q1 + q2);
    const ey = CELL.l1 * Math.sin(q1) + CELL.l2 * Math.sin(q1 + q2);
    halo.position.set(ex * S, ey * S, 0.07 * S);
    haloOuter.position.copy(halo.position);
    const ratio = a.c2 ? Math.min(a.c2[0] / 15, 3) : 0;
    const pulse = 1 + 0.08 * Math.sin(state.time * 6) * (a.c2 && a.c2[1] !== 'N' ? 1 : 0.3);
    halo.scale.setScalar((1 + ratio * 0.25) * pulse);
    haloOuter.scale.setScalar(1 + ratio * 0.35 + state.flash * 0.8);
    state.flash = Math.max(0, state.flash - dt * 1.5);

    // Opérateur : entre, travaille près du poste de prise, repart.
    const op = a.op;
    const opTarget = op ? op[0] : CELL.operator.center[0] + 0.7;
    state.opX += (opTarget - state.opX) * (snap ? 1 : 1 - Math.exp(-dt * 8));
    state.opVisible += ((op ? 1 : 0) - state.opVisible) * (snap ? 1 : 1 - Math.exp(-dt * 4));
    const opY = op ? op[1] : CELL.operator.center[1];
    operator.position.x = state.opX * S;
    operator.visible = state.opVisible > 0.02;
    opSphere.position.set(state.opX * S, opY * S, 0);
    collab.position.copy(opSphere.position);
    operatorLabel.position.set(state.opX * S, (GROUND_Y + 1.85) * S, operator.position.z);
    operatorLabel.material.opacity = 0.9 * state.opVisible;
  }

  function updateFocus(dt) {
    const s = state.stage;
    const e = reducedMotion ? 1 : 1 - Math.exp(-dt * 4);
    Object.keys(FOCUS).forEach((key) => {
      state.focus[key] += (FOCUS[key][s] - state.focus[key]) * e;
    });
    const F = state.focus;
    ghostMat.opacity = 0.08 + 0.3 * F.ghost;
    ghostMat.emissiveIntensity = 0.3 + 0.8 * F.ghost;
    haloMat.opacity = 0.35 + 0.6 * F.halo;
    haloOuterMat.opacity = 0.08 + 0.35 * F.halo + 0.4 * state.flash;
    gateMat.emissiveIntensity = 0.1 + 0.9 * F.gate;
    planMat.opacity = 0.35 + 0.65 * F.plan;
    planBeadMat.opacity = planMat.opacity;
    prunedMat.opacity = 0.2 + 0.7 * F.plan;
    prunedBeadMat.opacity = prunedMat.opacity;
    opSphereMat.opacity = (0.06 + 0.2 * F.scg) * state.opVisible;
    opSphereMat.emissiveIntensity = 0.2 + 0.6 * F.scg;
    collabMat.opacity = (0.04 + 0.16 * F.scg) * state.opVisible;
    fixtureShellMat.opacity = 0.08 + 0.4 * F.scg;
    blockMat.emissiveIntensity = 0.1 + 0.8 * F.ledger;
    chainLinkMat.opacity = 0.2 + 0.6 * F.ledger;
    // Les blocs du registre tournent doucement, le plus récent ressort.
    for (let i = 0; i < CHAIN; i += 1) {
      const lift = i === CHAIN - 1 ? 0.02 * S * (1 + Math.sin(state.time * 4)) : 0;
      m4.makeRotationY(state.time * 0.4 + i * 0.3).setPosition(chainStart.x + i * chainStep, chainStart.y + lift, chainStart.z);
      chain.setMatrixAt(i, m4);
    }
    chain.instanceMatrix.needsUpdate = true;
  }

  function updateCamera(dt, immediate = false) {
    const [p, l] = VIEWS[state.stage];
    const k = immediate ? 1 : 1 - Math.exp(-dt * 2.2);
    state.parallax.lerp(state.pointer, immediate ? 1 : Math.min(1, dt * 3));
    const idle = reducedMotion ? 0 : 1;
    tmp.set(
      (p[0] + state.parallax.x * 0.12 + Math.sin(state.time * 0.21) * 0.06 * idle) * S,
      (p[1] + state.parallax.y * 0.08 + Math.sin(state.time * 0.17) * 0.04 * idle) * S,
      p[2] * S,
    );
    tmpLook.set(l[0] * S, l[1] * S, l[2] * S);
    camPos.lerp(tmp, k);
    camLook.lerp(tmpLook, k);
    camera.position.copy(camPos);
    camera.lookAt(camLook);
  }

  /* --------------------------------------------------------- redimension */
  function resize() {
    const w = canvas.clientWidth || window.innerWidth;
    const h = canvas.clientHeight || window.innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // Grand écran : le texte occupe la gauche, la scène est décalée vers la droite.
    // Mobile : les cartes de texte occupent le centre, la scène remonte dans le tiers haut.
    if (w >= 960) camera.setViewOffset(w, h, -w * 0.2, 0, w, h);
    else camera.setViewOffset(w, h, 0, h * 0.24, w, h);
    camera.fov = w < 720 ? 58 : 42;
    camera.updateProjectionMatrix();
    if (!state.running) renderOnce();
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(canvas);

  /* ----------------------------------------------------------- survol */
  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const BLOCKERS = '.card, a, button, input, select, textarea, label, .site-header, .consent, .hero-copy, .scene-hud';

  function pick(event) {
    const rect = canvas.getBoundingClientRect();
    ndc.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const hit = raycaster.intersectObjects(pickables, false).find((h) => h.object.visible);
    return hit?.object ?? null;
  }

  function onPointerMove(event) {
    state.pointer.set((event.clientX / window.innerWidth) * 2 - 1, -(event.clientY / window.innerHeight) * 2 + 1);
    if (event.pointerType === 'touch' || !state.running) return;
    const blocked = event.target instanceof Element && event.target.closest(BLOCKERS);
    const hit = blocked ? null : pick(event);
    if (hit !== state.hovered) {
      state.hovered = hit;
      document.documentElement.classList.toggle('graph-hover', Boolean(hit));
    }
    if (tooltip) {
      if (hit) {
        const { node } = hit.userData;
        tooltip.replaceChildren();
        const strong = document.createElement('strong');
        strong.textContent = node.label;
        const span = document.createElement('span');
        span.textContent = node.desc;
        tooltip.append(strong, span);
        tooltip.style.transform = `translate(${event.clientX + 16}px, ${event.clientY + 16}px)`;
        tooltip.hidden = false;
      } else {
        tooltip.hidden = true;
      }
    }
  }

  function onClick(event) {
    if (!state.hovered) return;
    const blocked = event.target instanceof Element && event.target.closest(BLOCKERS);
    if (blocked) return;
    onNodeActivate?.(state.hovered.userData.node);
  }

  window.addEventListener('pointermove', onPointerMove, { passive: true });
  window.addEventListener('click', onClick);

  function onContextLostEvent(event) {
    event.preventDefault();
    onContextLost?.();
  }
  canvas.addEventListener('webglcontextlost', onContextLostEvent);

  /* ------------------------------------------------------------- boucle */
  let lastTime = performance.now();
  let raf = 0;
  let frames = 0;
  let frameTime = 0;
  let goodStreak = 0;

  function adaptQuality(dt) {
    frames += 1;
    frameTime += dt;
    if (frames < 90) return;
    const fps = frames / frameTime;
    frames = 0;
    frameTime = 0;
    if (fps < 52 && dpr > 0.75) {
      dpr = Math.max(0.75, dpr - 0.25);
      renderer.setPixelRatio(dpr);
      goodStreak = 0;
    } else if (fps > 58.5 && dpr < maxDpr) {
      goodStreak += 1;
      if (goodStreak >= 4) {
        dpr = Math.min(maxDpr, dpr + 0.25);
        renderer.setPixelRatio(dpr);
        goodStreak = 0;
      }
    }
  }

  function frame(now) {
    raf = requestAnimationFrame(frame);
    const dt = Math.min(Math.max((now - lastTime) / 1000, 0), 1 / 20);
    lastTime = now;
    state.time += dt;
    state.stageTime += dt;
    updateMission(dt);
    updateFocus(dt);
    updateCamera(dt);
    renderer.render(scene, camera);
    adaptQuality(dt);
  }

  function renderOnce() {
    if (state.disposed) return;
    // Mouvement réduit ou rendu suspendu : une image fixe et complète de l'étape courante.
    updateMission(0, true);
    for (let i = 0; i < 6; i += 1) updateFocus(1);
    updateCamera(0, true);
    renderer.render(scene, camera);
  }

  function setActive(active) {
    if (state.disposed) return;
    const shouldRun = active && !reducedMotion && document.visibilityState === 'visible';
    if (shouldRun === state.running) return;
    state.running = shouldRun;
    if (shouldRun) {
      lastTime = performance.now();
      raf = requestAnimationFrame(frame);
    } else {
      cancelAnimationFrame(raf);
      if (tooltip) tooltip.hidden = true;
      state.hovered = null;
      document.documentElement.classList.remove('graph-hover');
    }
  }

  function setStage(stage) {
    const next = THREE.MathUtils.clamp(stage | 0, 0, VIEWS.length - 1);
    if (next === state.stage) return;
    state.stage = next;
    state.stageTime = 0;
    if (reducedMotion) renderOnce();
  }

  function dispose() {
    if (state.disposed) return;
    setActive(false);
    state.disposed = true;
    aborted = true;
    controller.abort();
    if (hud) hud.hidden = true;
    resizeObserver.disconnect();
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('click', onClick);
    canvas.removeEventListener('webglcontextlost', onContextLostEvent);
    const textures = new Set();
    scene.traverse((obj) => {
      obj.geometry?.dispose();
      obj.dispose?.(); // InstancedMesh : libère ses attributs d'instance
      const materials = Array.isArray(obj.material) ? obj.material : obj.material ? [obj.material] : [];
      materials.forEach((m) => {
        if (m.map) textures.add(m.map);
        m.dispose();
      });
    });
    textures.forEach((t) => t.dispose());
    envTexture.dispose();
    scene.clear();
    renderer.dispose();
    renderer.forceContextLoss();
  }

  resize();
  renderOnce();
  return { setStage, setActive, dispose, get stage() { return state.stage; } };
}
