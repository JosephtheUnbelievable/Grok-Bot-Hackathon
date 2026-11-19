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
    color: 0x1a242c,
    roughness: 0.96,
    metalness: 0,
  });
  const photoMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
  const earth = new THREE.Mesh(new THREE.SphereGeometry(1, 80, 56), fallbackMat);
  scene.add(earth);

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const sun = new THREE.DirectionalLight(0xfff4e4, 1.45);
  sun.position.set(4, 1.4, 3);
  scene.add(sun);

  const iceMat = new THREE.MeshBasicMaterial({
    transparent: true,
    depthWrite: false,
  });
  const ice = new THREE.Mesh(new THREE.SphereGeometry(1.004, 80, 56), iceMat);
  ice.renderOrder = 2;
  ice.visible = false;
  scene.add(ice);
  scene.add(makeGraticule());
  const graticule = scene.children.at(-1);

  const loader = new THREE.TextureLoader();
  loader.setCrossOrigin("anonymous");

  let iceToken = 0;
  let iceTexture = null;
  let objectUrl = null;
  let anim = null;
  let running = true;

  function applyIce(texture, show) {
    if (iceTexture && iceTexture !== texture) iceTexture.dispose();
    iceTexture = texture;
    iceMat.map = texture;
    iceMat.needsUpdate = true;
    ice.visible = Boolean(show && texture);
  }

  function clearIce() {
    iceToken += 1;
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    applyIce(null, false);
  }

  function setIceCanvas(canvas) {
    iceToken += 1;
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 8;
    tex.needsUpdate = true;
    applyIce(tex, true);
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
        tex.anisotropy = 8;
        applyIce(tex, true);
      },
      undefined,
      () => {
        if (token === iceToken) applyIce(iceTexture, ice.visible);
      },
    );
  }

  function setBasemap(url, onDone) {
    loader.load(
      url,
      (tex) => {
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
        photoMat.map = tex;
        photoMat.needsUpdate = true;
        earth.material = photoMat;
        graticule.visible = false;
        onDone?.(true);
      },
      undefined,
      () => {
        earth.material = fallbackMat;
        graticule.visible = true;
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
  container.addEventListener("pointerdown", () => {
    anim = null;
  });

  function resize() {
    const w = container.clientWidth || 1;
    const h = container.clientHeight || 1;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }

  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();
  camera.position.copy(vectorFor(-32, 38, 2.85));
  controls.update();

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
    dispose() {
      running = false;
      observer.disconnect();
      renderer.dispose();
    },
  };
}

function makeGraticule() {
  const positions = [];
  const r = 1.003;
  const push = (lat, lon) => {
    const phi = ((lon + 180) * Math.PI) / 180;
    const theta = ((90 - lat) * Math.PI) / 180;
    positions.push(
      -Math.cos(phi) * Math.sin(theta) * r,
      Math.cos(theta) * r,
      Math.sin(phi) * Math.sin(theta) * r,
    );
  };
  for (let lat = -60; lat <= 60; lat += 30) {
    for (let lon = -180; lon < 180; lon += 5) {
      push(lat, lon);
      push(lat, lon + 5);
    }
  }
  for (let lon = -180; lon < 180; lon += 30) {
    for (let lat = -90; lat < 90; lat += 5) {
      push(lat, lon);
      push(Math.min(90, lat + 5), lon);
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  return new THREE.LineSegments(
    geometry,
    new THREE.LineBasicMaterial({ color: 0x7f97a3, transparent: true, opacity: 0.35 }),
  );
}
