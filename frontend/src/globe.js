import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { createShipLayer } from "../../ships/web/shipLayer.js";

export function createGlobe(container, { apiBase = "http://127.0.0.1:8000" } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.05, 40);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enablePan = false;
  controls.enableDamping = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  controls.dampingFactor = 0.08;
  controls.rotateSpeed = 0.55;
  controls.minDistance = 1.65;
  controls.maxDistance = 6;
  controls.minPolarAngle = 0;
  controls.maxPolarAngle = Math.PI;
  controls.target.set(0, 0, 0);

  const fallbackMat = new THREE.MeshStandardMaterial({
    color: 0x6d8b98,
    roughness: 0.9,
    metalness: 0,
  });
  const photoMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
  const earth = new THREE.Mesh(new THREE.SphereGeometry(1, 160, 120), fallbackMat);
  scene.add(earth);

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const sun = new THREE.DirectionalLight(0xfff4e4, 1.45);
  sun.position.set(4, 1.4, 3);
  scene.add(sun);

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function makeIceMesh() {
    const material = new THREE.MeshBasicMaterial({
      transparent: true,
      depthWrite: false,
      opacity: 0,
    });
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(1.003, 160, 120), material);
    mesh.renderOrder = 2;
    mesh.visible = false;
    scene.add(mesh);
    return mesh;
  }
  const iceA = makeIceMesh();
  const iceB = makeIceMesh();
  let shown = iceA;
  let queued = iceB;
  let fade = null;

  const atmosphere = new THREE.Mesh(
    new THREE.SphereGeometry(1.028, 80, 60),
    new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      side: THREE.BackSide,
      uniforms: { color: { value: new THREE.Color(0x9eb8c4) } },
      vertexShader: `
        varying vec3 vNormal;
        varying vec3 vView;
        void main() {
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          vNormal = normalize(normalMatrix * normal);
          vView = normalize(-mv.xyz);
          gl_Position = projectionMatrix * mv;
        }
      `,
      fragmentShader: `
        varying vec3 vNormal;
        varying vec3 vView;
        uniform vec3 color;
        void main() {
          float rim = pow(clamp(0.35 - dot(normalize(vNormal), normalize(vView)), 0.0, 1.0), 3.2);
          gl_FragColor = vec4(color, rim * 0.55);
        }
      `,
    }),
  );
  atmosphere.renderOrder = 1;
  scene.add(atmosphere);
  scene.add(makeStars());

  const graticule = makeGraticule();
  scene.add(graticule);

  let shipLayer = null;
  let shipPickHandler = null;
  try {
    shipLayer = createShipLayer({
      scene,
      latLonToVector3: (lat, lon, radius) => latLon(lat, lon, radius),
      apiBase,
      globeRadius: 1,
    });
  } catch (err) {
    console.error(err);
  }

  let pointerDown = null;
  renderer.domElement.addEventListener("pointerdown", (event) => {
    pointerDown = { x: event.clientX, y: event.clientY };
  });
  renderer.domElement.addEventListener("pointerup", (event) => {
    if (!pointerDown || !shipLayer) return;
    const dx = event.clientX - pointerDown.x;
    const dy = event.clientY - pointerDown.y;
    pointerDown = null;
    if (dx * dx + dy * dy > 25) return;
    const ship = shipLayer.pick(camera, event);
    shipLayer.select(ship ? ship.mmsi : null);
    shipPickHandler?.(ship, shipLayer.getState());
  });

  const loader = new THREE.TextureLoader();
  loader.setCrossOrigin("anonymous");

  let iceToken = 0;
  let iceTexture = null;
  let objectUrl = null;
  let anim = null;
  let running = true;

  function retire(mesh) {
    const map = mesh.material.map;
    mesh.visible = false;
    mesh.material.opacity = 0;
    mesh.material.map = null;
    mesh.material.needsUpdate = true;
    if (map) map.dispose();
  }

  function showIce(texture) {
    if (!texture) {
      fade = null;
      retire(iceA);
      retire(iceB);
      iceTexture = null;
      return;
    }
    const target = shown.visible ? queued : shown;
    if (target.material.map && target.material.map !== texture) target.material.map.dispose();
    target.material.map = texture;
    target.material.needsUpdate = true;
    target.visible = true;
    iceTexture = texture;
    if (reduceMotion || !shown.visible || shown === target) {
      if (shown !== target) retire(shown);
      target.material.opacity = 1;
      shown = target;
      queued = shown === iceA ? iceB : iceA;
      fade = null;
      return;
    }
    target.material.opacity = 0;
    fade = { from: shown, to: target, t0: performance.now() };
  }

  function clearIce() {
    iceToken += 1;
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    showIce(null);
  }

  function setIceCanvas(canvas) {
    iceToken += 1;
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
    tex.magFilter = THREE.LinearFilter;
    tex.minFilter = THREE.LinearMipmapLinearFilter;
    tex.needsUpdate = true;
    showIce(tex);
  }

  function setIceUrl(url) {
    const token = ++iceToken;
    if (objectUrl && objectUrl !== url) URL.revokeObjectURL(objectUrl);
    objectUrl = url;
    loader.load(
      url,
      (tex) => {
        if (token !== iceToken) {
          tex.dispose();
          return;
        }
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
        tex.magFilter = THREE.LinearFilter;
        tex.minFilter = THREE.LinearMipmapLinearFilter;
        showIce(tex);
      },
      undefined,
      () => {
        if (token === iceToken && iceTexture) showIce(iceTexture);
      },
    );
  }

  function setBasemap(url, onDone) {
    loader.load(
      url,
      (tex) => {
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
        tex.minFilter = THREE.LinearMipmapLinearFilter;
        tex.magFilter = THREE.LinearFilter;
        tex.generateMipmaps = true;
        photoMat.map = tex;
        photoMat.needsUpdate = true;
        earth.material = photoMat;
        onDone?.(true);
      },
      undefined,
      () => {
        earth.material = fallbackMat;
        onDone?.(false);
      },
    );
  }

  function vectorFor(lonDeg, latDeg, radius) {
    const phi = ((lonDeg + 180) * Math.PI) / 180;
    const theta = ((90 - latDeg) * Math.PI) / 180;
    return new THREE.Vector3(
      -Math.cos(phi) * Math.sin(theta) * radius,
      Math.cos(theta) * radius,
      Math.sin(phi) * Math.sin(theta) * radius,
    );
  }

  function look(lon, lat) {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const start = camera.position.clone();
    const end = vectorFor(lon, lat, start.length() || 2.8);
    if (reduce) {
      camera.position.copy(end);
      camera.lookAt(0, 0, 0);
      controls.update();
      anim = null;
      return;
    }
    anim = { start, end, t0: performance.now() };
  }

  function nudge(dPhi, dTheta) {
    anim = null;
    const spherical = new THREE.Spherical().setFromVector3(camera.position);
    spherical.phi = Math.min(Math.PI, Math.max(0.001, spherical.phi + dPhi));
    spherical.theta += dTheta;
    camera.position.setFromSpherical(spherical);
    controls.update();
  }

  container.addEventListener("keydown", (event) => {
    const step = 0.12;
    if (event.key === "ArrowUp") {
      event.preventDefault();
      nudge(-step, 0);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      nudge(step, 0);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      nudge(0, step);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      nudge(0, -step);
    }
  });
  let idleTimer = 0;
  function armIdle() {
    controls.autoRotate = false;
    window.clearTimeout(idleTimer);
    if (reduceMotion) return;
    idleTimer = window.setTimeout(() => {
      if (!anim) {
        controls.autoRotate = true;
        controls.autoRotateSpeed = 0.35;
      }
    }, 4000);
  }

  container.addEventListener("pointerdown", () => {
    anim = null;
    armIdle();
  });
  renderer.domElement.addEventListener("wheel", armIdle, { passive: true });
  container.addEventListener("keydown", armIdle);
  controls.addEventListener("start", armIdle);

  function fitRadius() {
    const vFov = (camera.fov * Math.PI) / 180;
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * Math.max(camera.aspect, 0.2));
    const margin = 1.06;
    return Math.max(margin / Math.tan(vFov / 2), margin / Math.tan(hFov / 2));
  }

  function resize() {
    const w = container.clientWidth || 1;
    const h = container.clientHeight || 1;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
    const spherical = new THREE.Spherical().setFromVector3(camera.position);
    if (!Number.isFinite(spherical.radius) || spherical.radius < 0.1) {
      spherical.set(fitRadius(), Math.PI / 2, 0);
    }
    spherical.radius = fitRadius();
    camera.position.setFromSpherical(spherical);
    controls.minDistance = spherical.radius * 0.98;
    controls.maxDistance = spherical.radius * 1.45;
    controls.update();
  }

  const observer = new ResizeObserver(resize);
  observer.observe(container);
  camera.position.copy(vectorFor(-32, 28, 4.2));
  resize();
  armIdle();

  function frame(now) {
    if (!running) return;
    requestAnimationFrame(frame);
    if (anim) {
      const u = Math.min(1, (now - anim.t0) / 720);
      const eased = u < 0.5 ? 2 * u * u : 1 - ((-2 * u + 2) ** 2) / 2;
      camera.position.lerpVectors(anim.start, anim.end, eased);
      camera.lookAt(0, 0, 0);
      if (u >= 1) {
        anim = null;
        controls.update();
      }
    } else {
      controls.update();
    }
    if (fade) {
      const u = reduceMotion ? 1 : Math.min(1, (now - fade.t0) / 420);
      fade.from.material.opacity = 1 - u;
      fade.to.material.opacity = u;
      if (u >= 1) {
        retire(fade.from);
        fade.to.material.opacity = 1;
        shown = fade.to;
        queued = shown === iceA ? iceB : iceA;
        fade = null;
      }
    }
    if (!reduceMotion) shipLayer?.update(now);
    renderer.render(scene, camera);
  }
  requestAnimationFrame(frame);

  return {
    setBasemap,
    setIceCanvas,
    setIceUrl,
    clearIce,
    lookNorth() {
      look(-40, 89);
    },
    lookSouth() {
      look(0, -89);
    },
    setShipsVisible(on) {
      shipLayer?.setVisible(on);
      if (!on) {
        shipLayer?.select(null);
        shipPickHandler?.(null, shipLayer?.getState());
      }
    },
    onShipPick(handler) {
      shipPickHandler = handler;
    },
    onShips(handler) {
      if (!shipLayer) return;
      shipLayer.onChange = handler;
      handler(shipLayer.getState());
    },
    selectShip(mmsi) {
      shipLayer?.select(mmsi);
    },
    shipState() {
      return shipLayer ? shipLayer.getState() : null;
    },
    dispose() {
      running = false;
      shipLayer?.dispose();
      observer.disconnect();
      renderer.dispose();
    },
  };
}

function latLon(lat, lon, radius) {
  const phi = ((lon + 180) * Math.PI) / 180;
  const theta = ((90 - lat) * Math.PI) / 180;
  return new THREE.Vector3(
    -Math.cos(phi) * Math.sin(theta) * radius,
    Math.cos(theta) * radius,
    Math.sin(phi) * Math.sin(theta) * radius,
  );
}

function makeGraticule() {
  const positions = [];
  const r = 1.008;
  const push = (lat, lon) => {
    const p = latLon(lat, lon, r);
    positions.push(p.x, p.y, p.z);
  };
  const parallels = [-66.5, -60, -30, 0, 30, 60, 66.5];
  for (const lat of parallels) {
    for (let lon = -180; lon < 180; lon += 8) {
      push(lat, lon);
      push(lat, lon + 8);
    }
  }
  for (let lon = -180; lon < 180; lon += 30) {
    for (let lat = -90; lat < 90; lat += 6) {
      push(lat, lon);
      push(Math.min(90, lat + 6), lon);
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  const lines = new THREE.LineSegments(
    geometry,
    new THREE.LineBasicMaterial({ color: 0xc5d0cc, transparent: true, opacity: 0.07, depthWrite: false }),
  );
  lines.renderOrder = 3;
  return lines;
}

function starShell(count, radiusMin, radiusSpan) {
  const positions = new Float32Array(count * 3);
  for (let i = 0; i < count; i += 1) {
    const radius = radiusMin + Math.random() * radiusSpan;
    const theta = Math.acos(2 * Math.random() - 1);
    const phi = Math.random() * Math.PI * 2;
    positions[i * 3] = radius * Math.sin(theta) * Math.cos(phi);
    positions[i * 3 + 1] = radius * Math.cos(theta);
    positions[i * 3 + 2] = radius * Math.sin(theta) * Math.sin(phi);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  return geometry;
}

function starSprite() {
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  const mid = size / 2;
  const gradient = ctx.createRadialGradient(mid, mid, 0, mid, mid, mid);
  gradient.addColorStop(0, "rgba(255,255,255,1)");
  gradient.addColorStop(0.12, "rgba(255,255,255,0.95)");
  gradient.addColorStop(0.28, "rgba(255,255,255,0.28)");
  gradient.addColorStop(0.55, "rgba(255,255,255,0.06)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.NoColorSpace;
  texture.needsUpdate = true;
  return texture;
}

function addStars(sky, sprite, { count, size, color, opacity }) {
  sky.add(new THREE.Points(
    starShell(count, 15, 14),
    new THREE.PointsMaterial({
      map: sprite,
      color,
      size,
      opacity,
      sizeAttenuation: false,
      depthWrite: false,
      transparent: true,
      blending: THREE.AdditiveBlending,
    }),
  ));
}

function makeStars() {
  const sky = new THREE.Group();
  const sprite = starSprite();
  addStars(sky, sprite, { count: 520, size: 4.5, color: 0xe7eefc, opacity: 0.72 });
  addStars(sky, sprite, { count: 220, size: 7, color: 0xc9dcff, opacity: 0.85 });
  addStars(sky, sprite, { count: 90, size: 10, color: 0xffffff, opacity: 0.95 });
  addStars(sky, sprite, { count: 70, size: 11, color: 0xffe2a8, opacity: 0.9 });
  addStars(sky, sprite, { count: 48, size: 8, color: 0xffb06a, opacity: 0.88 });
  return sky;
}
