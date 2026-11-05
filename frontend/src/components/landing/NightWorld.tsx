/**
 * The front page's world: a night airspace over a protected site, in WebGL.
 *
 * Dotted terrain falls away into fog. Range rings sit on the ground around the site, a sweep
 * goes round, and a faint dome marks the protected volume. Four contacts move through the sky
 * with fading trails; one is closing on the dome and is drawn in the warning colour. The
 * camera eases in from high above on load and then leans gently toward the pointer.
 *
 * Loaded lazily (`React.lazy`), so three.js is only fetched by someone who opens the front
 * page, never by the console. Every colour comes from the design tokens. With reduced motion
 * it renders one still frame and stops. It pauses while the tab is hidden.
 */

import { useEffect, useRef } from 'react'
import * as THREE from 'three'

interface Props {
  still: boolean
}

export default function NightWorld({ still }: Props) {
  const hostRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'low-power' })
    } catch {
      return // No WebGL: the CSS sky behind this element is the whole picture.
    }

    const css = getComputedStyle(document.documentElement)
    const token = (name: string, fallback: string) =>
      new THREE.Color(css.getPropertyValue(name).trim() || fallback)
    const sky = token('--sky-top', '#050407')
    const plum = token('--brand-accent', '#c39bd3')
    const plumDeep = token('--brand-mid', '#8a6290')
    const warning = token('--status-warning', '#e8ad52')
    const zone = token('--zone-protected', '#f26d66')
    const ink = token('--text-primary', '#ede6ef')

    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75))
    renderer.setClearColor(sky, 1)
    host.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    scene.fog = new THREE.FogExp2(sky, 0.022)

    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 400)
    const restPosition = new THREE.Vector3(0, 13, 40)
    const introPosition = new THREE.Vector3(0, 46, 70)
    const lookAt = new THREE.Vector3(0, 3, 0)
    camera.position.copy(still ? restPosition : introPosition)
    camera.lookAt(lookAt)

    // --- Terrain: a field of points over gently rolling ground. ------------------------
    const size = 260
    const segments = 180
    const ground = new THREE.PlaneGeometry(size, size, segments, segments)
    ground.rotateX(-Math.PI / 2)
    const positions = ground.attributes.position as THREE.BufferAttribute
    const colours = new Float32Array(positions.count * 3)
    const low = plumDeep.clone().multiplyScalar(0.35)
    const high = plum.clone().multiplyScalar(0.8)
    const tmp = new THREE.Color()
    for (let i = 0; i < positions.count; i++) {
      const x = positions.getX(i)
      const z = positions.getZ(i)
      const r = Math.hypot(x, z)
      // Flat around the site, rising into hills with distance.
      const lift = Math.min(1, Math.max(0, (r - 18) / 40))
      const h =
        lift *
        (2.6 * Math.sin(x * 0.07) * Math.cos(z * 0.06) +
          1.4 * Math.sin(x * 0.17 + z * 0.11) +
          0.6 * Math.cos(z * 0.29 - x * 0.05))
      positions.setY(i, h - 0.4)
      tmp.copy(low).lerp(high, Math.min(1, Math.max(0, (h + 2) / 6)))
      colours.set([tmp.r, tmp.g, tmp.b], i * 3)
    }
    ground.setAttribute('color', new THREE.BufferAttribute(colours, 3))
    const terrain = new THREE.Points(
      ground,
      new THREE.PointsMaterial({
        size: 0.12,
        vertexColors: true,
        transparent: true,
        opacity: 0.85,
        depthWrite: false,
      }),
    )
    scene.add(terrain)

    // --- Range rings, spokes and the protected dome. -------------------------------------
    const ringMaterial = new THREE.LineBasicMaterial({
      color: plum,
      transparent: true,
      opacity: 0.22,
    })
    for (const radius of [8, 16, 24, 32, 44]) {
      const curve = new THREE.EllipseCurve(0, 0, radius, radius)
      const pts = curve.getPoints(160).map((p) => new THREE.Vector3(p.x, 0.02, p.y))
      scene.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), ringMaterial))
    }
    const spokeMaterial = new THREE.LineBasicMaterial({ color: plum, transparent: true, opacity: 0.08 })
    for (let a = 0; a < 12; a++) {
      const t = (a / 12) * Math.PI * 2
      scene.add(
        new THREE.Line(
          new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(Math.cos(t) * 8, 0.02, Math.sin(t) * 8),
            new THREE.Vector3(Math.cos(t) * 44, 0.02, Math.sin(t) * 44),
          ]),
          spokeMaterial,
        ),
      )
    }

    const dome = new THREE.Mesh(
      new THREE.SphereGeometry(6, 32, 12, 0, Math.PI * 2, 0, Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: zone, wireframe: true, transparent: true, opacity: 0.16 }),
    )
    scene.add(dome)
    const domeBase = new THREE.Mesh(
      new THREE.RingGeometry(5.9, 6.05, 96),
      new THREE.MeshBasicMaterial({ color: zone, transparent: true, opacity: 0.6, side: THREE.DoubleSide }),
    )
    domeBase.rotation.x = -Math.PI / 2
    domeBase.position.y = 0.03
    scene.add(domeBase)

    // --- The sweep: a wedge of light whose brightness falls off behind its leading edge. ---
    const sweepGeometry = new THREE.CircleGeometry(44, 64, 0, Math.PI / 3)
    sweepGeometry.rotateX(-Math.PI / 2)
    const sweepMaterial = new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      uniforms: { uColour: { value: plum } },
      vertexShader: `
        varying vec3 vPos;
        void main() { vPos = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
      `,
      fragmentShader: `
        uniform vec3 uColour;
        varying vec3 vPos;
        void main() {
          float a = atan(-vPos.z, vPos.x);            // 0 at the trailing edge
          float lead = clamp(a / 1.0472, 0.0, 1.0);    // 1 at the leading edge (pi/3)
          float r = length(vPos.xz) / 44.0;
          float alpha = pow(lead, 3.0) * 0.22 * smoothstep(1.0, 0.2, r);
          gl_FragColor = vec4(uColour, alpha);
        }
      `,
    })
    const sweep = new THREE.Mesh(sweepGeometry, sweepMaterial)
    sweep.position.y = 0.04
    scene.add(sweep)

    // --- Contacts with fading trails. ---------------------------------------------------
    const glow = makeGlowTexture()
    interface Contact {
      sprite: THREE.Sprite
      trail: THREE.Line
      history: THREE.Vector3[]
      path: (t: number) => THREE.Vector3
    }
    const TRAIL = 60
    const contacts: Contact[] = []
    const addContact = (colour: THREE.Color, scale: number, path: (t: number) => THREE.Vector3) => {
      const sprite = new THREE.Sprite(
        new THREE.SpriteMaterial({
          map: glow,
          color: colour,
          transparent: true,
          depthWrite: false,
          blending: THREE.AdditiveBlending,
        }),
      )
      sprite.scale.setScalar(scale)
      const trailGeometry = new THREE.BufferGeometry()
      trailGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(TRAIL * 3), 3))
      const fade = new Float32Array(TRAIL * 3)
      for (let i = 0; i < TRAIL; i++) {
        const k = i / (TRAIL - 1)
        fade.set([colour.r * k, colour.g * k, colour.b * k], i * 3)
      }
      trailGeometry.setAttribute('color', new THREE.BufferAttribute(fade, 3))
      const trail = new THREE.Line(
        trailGeometry,
        new THREE.LineBasicMaterial({
          vertexColors: true,
          transparent: true,
          opacity: 0.8,
          blending: THREE.AdditiveBlending,
        }),
      )
      scene.add(sprite, trail)
      contacts.push({ sprite, trail, history: [], path })
    }
    // One closing on the dome, in the warning colour.
    addContact(warning, 1.6, (t) => {
      const k = (t * 0.035) % 1
      const r = 46 - k * 40
      const a = -0.9 + k * 0.5
      return new THREE.Vector3(Math.cos(a) * r, 4 + 3 * (1 - k), Math.sin(a) * r)
    })
    addContact(ink, 1.1, (t) => new THREE.Vector3(Math.cos(t * 0.05) * 30, 7, Math.sin(t * 0.05) * 22))
    addContact(plum, 1.1, (t) => new THREE.Vector3(-36 + ((t * 1.4) % 72), 10, -14 + Math.sin(t * 0.3) * 3))
    addContact(plum, 0.9, (t) => new THREE.Vector3(Math.sin(t * 0.08) * 18 + 12, 5.5, Math.cos(t * 0.08) * 12 - 20))

    // --- Pointer, resize, visibility. -----------------------------------------------------
    let pointerX = 0
    let pointerY = 0
    const onPointer = (e: PointerEvent) => {
      pointerX = (e.clientX / window.innerWidth - 0.5) * 2
      pointerY = (e.clientY / window.innerHeight - 0.5) * 2
    }
    window.addEventListener('pointermove', onPointer)

    const resize = () => {
      const w = host.clientWidth || window.innerWidth
      const h = host.clientHeight || window.innerHeight
      renderer.setSize(w, h, false)
      camera.aspect = w / h
      // Narrow screens pull back so the site still fits.
      camera.fov = w < 700 ? 52 : 38
      camera.updateProjectionMatrix()
    }
    const observer = new ResizeObserver(resize)
    observer.observe(host)
    resize()

    const clock = new THREE.Clock()
    let frame = 0
    const target = new THREE.Vector3()

    const render = () => {
      const t = clock.getElapsedTime()
      const intro = still ? 1 : easeOutExpo(Math.min(1, t / 3.2))

      target.copy(introPosition).lerp(restPosition, intro)
      target.x += pointerX * 4
      target.y += -pointerY * 1.6
      camera.position.lerp(target, still ? 1 : 0.06)
      camera.lookAt(lookAt)

      sweep.rotation.y = -t * 0.55
      for (const c of contacts) {
        const p = c.path(t + 8)
        c.sprite.position.copy(p)
        c.history.push(p.clone())
        if (c.history.length > TRAIL) c.history.shift()
        const attr = c.trail.geometry.attributes.position as THREE.BufferAttribute
        for (let i = 0; i < TRAIL; i++) {
          const h = c.history[Math.max(0, c.history.length - TRAIL + i)] ?? p
          attr.setXYZ(i, h.x, h.y, h.z)
        }
        attr.needsUpdate = true
      }
      renderer.render(scene, camera)
    }

    const loop = () => {
      render()
      frame = window.requestAnimationFrame(loop)
    }

    const onVisibility = () => {
      window.cancelAnimationFrame(frame)
      if (!document.hidden && !still) frame = window.requestAnimationFrame(loop)
    }
    document.addEventListener('visibilitychange', onVisibility)

    if (still) {
      // Let the contacts lay down their trails, then hold one frame.
      for (let i = 0; i < TRAIL; i++) render()
    } else {
      frame = window.requestAnimationFrame(loop)
    }

    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('pointermove', onPointer)
      document.removeEventListener('visibilitychange', onVisibility)
      observer.disconnect()
      scene.traverse((object) => {
        const mesh = object as THREE.Mesh
        mesh.geometry?.dispose()
        const material = mesh.material as THREE.Material | THREE.Material[] | undefined
        if (Array.isArray(material)) material.forEach((m) => m.dispose())
        else material?.dispose()
      })
      glow.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [still])

  return <div ref={hostRef} className="night-world" aria-hidden="true" />
}

function easeOutExpo(x: number): number {
  return x >= 1 ? 1 : 1 - Math.pow(2, -10 * x)
}

/** A soft round point of light, drawn once on a canvas. */
function makeGlowTexture(): THREE.Texture {
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 64
  const g = canvas.getContext('2d')
  if (g) {
    const gradient = g.createRadialGradient(32, 32, 0, 32, 32, 32)
    gradient.addColorStop(0, 'rgba(255,255,255,1)')
    gradient.addColorStop(0.18, 'rgba(255,255,255,0.85)')
    gradient.addColorStop(0.45, 'rgba(255,255,255,0.18)')
    gradient.addColorStop(1, 'rgba(255,255,255,0)')
    g.fillStyle = gradient
    g.fillRect(0, 0, 64, 64)
  }
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  return texture
}
