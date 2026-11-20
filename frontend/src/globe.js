import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

export function createGlobe(container) {
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

  const nsr = makeRoute(73, 30, 180);
  const nwp = makeRoute(71, -140, -60);
  scene.add(nsr);
  scene.add(nwp);

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

  function setRoutes(nsrDays, nwpDays) {
    paintRoute(nsr, nsrDays);
    paintRoute(nwp, nwpDays);
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
    renderer.render(scene, camera);
  }
  requestAnimationFrame(frame);

  return {
    setBasemap,
    setIceCanvas,
    setIceUrl,
    clearIce,
    setRoutes,
    lookNorth() {
      look(-40, 89);
    },
    lookSouth() {
      look(0, -89);
    },
    dispose() {
      running = false;
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

function addStars(sky, { count, size, color, opacity, glow }) {
  const geometry = starShell(count, 15, 14);
  const shared = {
    sizeAttenuation: false,
    depthWrite: false,
    transparent: true,
    blending: THREE.AdditiveBlending,
  };
  sky.add(new THREE.Points(geometry, new THREE.PointsMaterial({
    ...shared,
    color: glow,
    size: size * 2.8,
    opacity: opacity * 0.55,
  })));
  sky.add(new THREE.Points(geometry, new THREE.PointsMaterial({
    ...shared,
    color,
    size,
    opacity,
  })));
}

function makeStars() {
  const sky = new THREE.Group();
  addStars(sky, { count: 48, size: 3.2, color: 0xffffff, opacity: 1, glow: 0xd7e6ff });
  addStars(sky, { count: 90, size: 2.2, color: 0xd5e6ff, opacity: 0.95, glow: 0x9eb6ff });
  addStars(sky, { count: 36, size: 3.5, color: 0xffe7b8, opacity: 1, glow: 0xffb15a });
  addStars(sky, { count: 28, size: 2.6, color: 0xffc48a, opacity: 0.95, glow: 0xff8a4a });
  return sky;
}

function makeRoute(lat, lon0, lon1) {
  const points = [];
  const steps = 40;
  for (let i = 0; i <= steps; i += 1) {
    const lon = lon0 + ((lon1 - lon0) * i) / steps;
    points.push(latLon(lat, lon, 1.012));
  }
  const curve = new THREE.CatmullRomCurve3(points);
  const mesh = new THREE.Mesh(
    new THREE.TubeGeometry(curve, 48, 0.008, 5, false),
    new THREE.MeshBasicMaterial({
      color: 0xd5e6ee,
      transparent: true,
      opacity: 0,
      depthWrite: false,
    }),
  );
  mesh.renderOrder = 4;
  mesh.visible = false;
  return mesh;
}

function paintRoute(mesh, days) {
  const n = Number(days);
  const opacity = Number.isFinite(n) ? Math.max(0, Math.min(0.8, n / 180)) : 0;
  mesh.material.opacity = opacity;
  mesh.visible = opacity > 0.02;
}
