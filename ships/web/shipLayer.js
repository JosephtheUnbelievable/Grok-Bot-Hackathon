/**
 * Arctic ship layer for a Three.js globe.
 *
 * Peer dependency: three. No imports from frontend/.
 *
 *   import { createShipLayer } from "./shipLayer.js";
 *   const ships = createShipLayer({ scene, latLonToVector3, apiBase, globeRadius });
 *   ships.onPick = (ship) => show(ship);
 */
import * as THREE from "three";

export const VESSEL_COLORS = {
  cargo: "#f4d03f",
  tanker: "#e74c3c",
  passenger: "#5dade2",
  fishing: "#27ae60",
  tug: "#af7ac5",
  pleasure: "#85929e",
  high_speed: "#e67e22",
  special: "#d5d8dc",
  unknown: "#7f8c8d",
};

const RISK_COLORS = {
  marginal: "#f5b041",
  heavy: "#c0392b",
};

export function shipFacts(ship, envelopeStale = false) {
  const risk = ship?.ice_risk || {};
  const rows = [
    ["Name", ship?.name || "Unnamed"],
    ["MMSI", ship?.mmsi || ""],
    ["Type", ship?.vessel_type || "unknown"],
    ["Speed", ship?.sog_kn == null ? "n/a" : `${ship.sog_kn} kn`],
    ["Course", ship?.cog_deg == null ? "n/a" : `${ship.cog_deg}°`],
    ["Heading", ship?.heading == null ? "n/a" : `${ship.heading}°`],
    ["Last seen", ship?.last_seen || "n/a"],
    ["Ice", risk.level || "unknown"],
    ["Concentration here", percent(risk.concentration_here)],
    ["Concentration ahead", percent(risk.concentration_ahead)],
    ["Icebreaker", ship?.icebreaker ? "yes" : "no"],
  ];
  let note = "";
  if (envelopeStale) note = "Position is last known. The feed is interrupted.";
  else if (ship?.is_stale) note = "Position is last known.";
  return {
    rows,
    note,
    color: VESSEL_COLORS[ship?.vessel_type] || VESSEL_COLORS.unknown,
  };
}

function percent(value) {
  return value == null || Number.isNaN(Number(value)) ? "n/a" : `${value}%`;
}

export function createShipLayer({
  scene,
  latLonToVector3,
  apiBase = "",
  globeRadius = 1,
  pollMs = 15000,
  allowPreview = false,
  previewShips = null,
} = {}) {
  if (!scene || typeof latLonToVector3 !== "function") {
    throw new Error("createShipLayer needs scene and latLonToVector3");
  }

  const group = new THREE.Group();
  group.name = "ships";
  scene.add(group);

  const markerGeo = new THREE.SphereGeometry(Math.max(globeRadius * 0.012, 0.012), 12, 8);
  const pickGeo = new THREE.SphereGeometry(Math.max(globeRadius * 0.045, 0.045), 8, 6);
  const ringGeo = new THREE.RingGeometry(globeRadius * 0.02, globeRadius * 0.028, 28);
  const pickables = [];
  const heavyRings = [];
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  let timer = 0;
  let visible = true;
  let disposed = false;
  let selected = null;
  let lastShips = [];
  let state = {
    ok: false,
    fixture: false,
    stale: false,
    source: "",
    count: 0,
    ships: [],
    error: null,
    health: null,
    preview: false,
  };

  const layer = {
    object: group,
    onPick: null,
    onChange: null,
    update,
    pick,
    refresh,
    select,
    setVisible,
    getState: () => ({ ...state, ships: state.ships.slice() }),
    dispose,
  };

  refresh();
  timer = window.setInterval(refresh, pollMs);
  return layer;

  function setVisible(on) {
    visible = Boolean(on);
    group.visible = visible;
  }

  function select(mmsi) {
    selected = mmsi || null;
    draw(lastShips);
  }

  function update(timeMs) {
    const pulse = 1 + 0.22 * Math.sin((timeMs || 0) / 220);
    for (const ring of heavyRings) ring.scale.setScalar(pulse);
  }

  function pick(camera, event) {
    if (!visible || !camera || !event) return null;
    const element = event.target;
    if (!element || !element.getBoundingClientRect) return null;
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(pickables, false);
    const view = new THREE.Vector3();
    camera.getWorldDirection(view);
    const hit = hits.find((item) => item.point.clone().normalize().dot(view) < -0.05);
    if (!hit) return null;
    const list = hit.object.userData.ships || [];
    return list[hit.instanceId] || null;
  }

  async function refresh() {
    if (disposed) return;
    const base = String(apiBase || "").replace(/\/$/, "");
    try {
      const [shipsRes, healthRes] = await Promise.all([
        fetch(`${base}/ships`, { cache: "no-store" }),
        fetch(`${base}/ships/health`, { cache: "no-store" }),
      ]);
      if (!shipsRes.ok) throw new Error(`ships ${shipsRes.status}`);
      const body = await shipsRes.json();
      if (body.fixture || body.source === "recorded-sample") {
        throw new Error("recorded sample refused");
      }
      let health = null;
      if (healthRes.ok) {
        health = await healthRes.json();
        if (health.fixture || health.using_sample) health = null;
      }
      const ships = Array.isArray(body.ships) ? body.ships : [];
      const usePreview = allowPreview && ships.length === 0 && Array.isArray(previewShips);
      state = {
        ok: true,
        fixture: false,
        stale: Boolean(body.stale),
        source: body.source || "",
        count: usePreview ? previewShips.length : ships.length,
        ships: usePreview ? previewShips : ships,
        error: null,
        health,
        preview: usePreview,
      };
    } catch (err) {
      const usePreview = allowPreview && Array.isArray(previewShips) && previewShips.length > 0;
      state = {
        ...state,
        ok: false,
        error: err && err.message ? err.message : "ship feed unavailable",
        ships: usePreview ? previewShips : [],
        count: usePreview ? previewShips.length : 0,
        preview: usePreview,
        stale: usePreview ? false : state.stale,
      };
    }
    lastShips = state.ships;
    draw(lastShips);
    if (typeof layer.onChange === "function") layer.onChange(layer.getState());
  }

  function draw(ships) {
    clearDrawn();
    if (!Array.isArray(ships) || ships.length === 0) return;
    const fresh = [];
    const stale = [];
    for (const ship of ships) {
      if (!Number.isFinite(Number(ship.lat)) || !Number.isFinite(Number(ship.lon))) continue;
      (ship.is_stale ? stale : fresh).push(ship);
      drawTrack(ship);
      drawRing(ship);
    }
    addMarkers(fresh, 1);
    addMarkers(stale, 0.4);
  }

  function addMarkers(ships, opacity) {
    if (!ships.length) return;
    const material = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: opacity < 1,
      opacity,
      depthWrite: false,
    });
    const mesh = new THREE.InstancedMesh(markerGeo, material, ships.length);
    mesh.userData.ships = ships;
    mesh.renderOrder = 6;
    const pickMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false });
    const picks = new THREE.InstancedMesh(pickGeo, pickMat, ships.length);
    picks.userData.ships = ships;
    const dummy = new THREE.Object3D();
    ships.forEach((ship, index) => {
      const pos = at(ship.lat, ship.lon, 1.02);
      dummy.position.copy(pos || new THREE.Vector3());
      dummy.scale.setScalar(pos ? 1 : 0);
      dummy.rotation.set(0, 0, 0);
      dummy.updateMatrix();
      mesh.setMatrixAt(index, dummy.matrix);
      picks.setMatrixAt(index, dummy.matrix);
      mesh.setColorAt(index, new THREE.Color(VESSEL_COLORS[ship.vessel_type] || VESSEL_COLORS.unknown));
    });
    mesh.instanceMatrix.needsUpdate = true;
    picks.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    group.add(mesh);
    group.add(picks);
    pickables.push(picks);
  }

  function drawTrack(ship) {
    const runs = splitTrack(ship.track);
    const color = VESSEL_COLORS[ship.vessel_type] || VESSEL_COLORS.unknown;
    for (const run of runs) {
      const points = [];
      for (const point of run) {
        const pos = at(point.lat, point.lon, 1.016);
        if (pos) points.push(pos);
      }
      if (points.length < 2) continue;
      const geometry = new THREE.BufferGeometry().setFromPoints(points);
      const material = new THREE.LineBasicMaterial({
        color,
        transparent: true,
        opacity: ship.is_stale ? 0.35 : 0.75,
        depthWrite: false,
      });
      const line = new THREE.Line(geometry, material);
      line.userData.ownGeometry = true;
      line.renderOrder = 5;
      group.add(line);
    }
  }

  function drawRing(ship) {
    const level = ship.ice_risk && ship.ice_risk.level;
    const chosen = ship.mmsi === selected;
    if (level !== "marginal" && level !== "heavy" && !chosen) return;
    const pos = at(ship.lat, ship.lon, 1.03);
    if (!pos) return;
    const normal = pos.clone().normalize();
    const color = chosen && level !== "heavy" && level !== "marginal"
      ? "#e6e1d6"
      : RISK_COLORS[level] || "#e6e1d6";
    const material = new THREE.MeshBasicMaterial({
      color,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: ship.is_stale ? 0.45 : 0.95,
      depthWrite: false,
    });
    const ring = new THREE.Mesh(ringGeo, material);
    ring.position.copy(pos);
    ring.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal);
    ring.renderOrder = 7;
    group.add(ring);
    if (level === "heavy") heavyRings.push(ring);
  }

  function at(lat, lon, scale) {
    try {
      const point = latLonToVector3(Number(lat), Number(lon), globeRadius * scale);
      if (!point || !Number.isFinite(point.x)) return null;
      return point;
    } catch {
      return null;
    }
  }

  function clearDrawn() {
    heavyRings.length = 0;
    pickables.length = 0;
    for (const child of group.children.slice()) {
      group.remove(child);
      if (child.userData.ownGeometry && child.geometry) child.geometry.dispose();
      const materials = Array.isArray(child.material) ? child.material : [child.material];
      materials.forEach((material) => material && material.dispose());
    }
  }

  function dispose() {
    disposed = true;
    window.clearInterval(timer);
    clearDrawn();
    markerGeo.dispose();
    pickGeo.dispose();
    ringGeo.dispose();
    scene.remove(group);
  }
}

function splitTrack(track) {
  if (!Array.isArray(track)) return [];
  const runs = [[]];
  for (const point of track) {
    if (!point || !Number.isFinite(Number(point.lat)) || !Number.isFinite(Number(point.lon))) continue;
    const run = runs[runs.length - 1];
    const prev = run[run.length - 1];
    if (prev && Math.abs(Number(point.lon) - Number(prev.lon)) > 180) runs.push([]);
    runs[runs.length - 1].push(point);
  }
  return runs.filter((run) => run.length > 1);
}
