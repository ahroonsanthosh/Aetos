/**
 * The material that makes a volume read as a volume.
 *
 * Protected zones and sensor coverage are both "a translucent region of space that has a
 * state". A flat translucent solid reads as a coloured box; what makes it read as a
 * holographic volume is that its *surfaces* are nearly invisible while its *edges* and
 * grazing angles are bright. That is fresnel, and it is doing almost all the work here.
 *
 * Everything is additive over a dark scene, which is why the palette stays desaturated:
 * two overlapping volumes must still look deliberate rather than blown out.
 */

import { AdditiveBlending, Color, DoubleSide, ShaderMaterial } from 'three';

const vertexShader = /* glsl */ `
  varying vec3 vNormalW;
  varying vec3 vViewW;
  varying float vUp;

  uniform float uHeight;

  void main() {
    vec4 world = modelMatrix * vec4(position, 1.0);
    vNormalW = normalize(mat3(modelMatrix) * normal);
    vViewW = normalize(cameraPosition - world.xyz);
    // Normalised height through the extrusion, for rim and sweep placement.
    vUp = clamp(position.y / max(uHeight, 0.0001), 0.0, 1.0);
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

const fragmentShader = /* glsl */ `
  varying vec3 vNormalW;
  varying vec3 vViewW;
  varying float vUp;

  uniform vec3  uColor;
  uniform float uOpacity;
  uniform float uTime;
  uniform float uSweep;    // 0 = still, 1 = a band travelling up the volume
  uniform float uRim;      // strength of the floor and ceiling rims

  void main() {
    // Grazing angles glow; surfaces facing the camera nearly vanish. This is the whole
    // trick — it is what stops the volume looking like tinted glass.
    //
    // The exponent is high on purpose. At 2.6 a large flat face still carries enough
    // fresnel across its middle to read as a solid pane, and a site-sized zone then
    // becomes a glass slab sitting over the hologram. At 4.2 the glow collapses back
    // onto the silhouette, which is what a volume should look like.
    float facing  = abs(dot(normalize(vNormalW), normalize(vViewW)));
    float fresnel = pow(1.0 - facing, 4.2);

    // Floor and ceiling read as drawn boundaries rather than cut edges.
    //
    // Gated on the surface being vertical. An extruded polygon's top cap lies entirely at
    // vUp = 1, so without this the rim lights the whole cap and the volume becomes an
    // opaque lid — the rim is meant to be the line where the wall ends, not the lid
    // itself. Caps are left to fresnel alone, which is what makes them read as open.
    float vertical  = 1.0 - abs(normalize(vNormalW).y);
    float rimTop    = smoothstep(0.90, 1.0, vUp);
    float rimBottom = smoothstep(0.10, 0.0, vUp);
    float rim = (rimTop * 1.5 + rimBottom * 0.9) * uRim * vertical;

    // A slow band climbing the volume. Only enabled when the zone is under threat, so
    // motion in the scene always means something.
    float s = fract(vUp * 0.9 - uTime * 0.09);
    float band = smoothstep(0.0, 0.03, s) * smoothstep(0.10, 0.05, s) * uSweep;

    // The constant term is near zero: a volume's interior contributes almost nothing, and
    // what the eye picks up is silhouette, rims and the sweep. Anything more and a large
    // zone dominates the scene it is supposed to sit inside.
    float alpha = uOpacity * (0.03 + fresnel * 1.5 + rim + band * 0.55);
    vec3  rgb   = uColor * (0.5 + fresnel * 0.95 + rim * 0.55 + band * 0.85);

    gl_FragColor = vec4(rgb, clamp(alpha, 0.0, 1.0));
  }
`;

export interface VolumeMaterialOptions {
  color: string;
  opacity: number;
  height: number;
  sweep?: number;
  rim?: number;
}

export function createVolumeMaterial(options: VolumeMaterialOptions): ShaderMaterial {
  return new ShaderMaterial({
    vertexShader,
    fragmentShader,
    uniforms: {
      uColor: { value: new Color(options.color) },
      uOpacity: { value: options.opacity },
      uHeight: { value: options.height },
      uTime: { value: 0 },
      uSweep: { value: options.sweep ?? 0 },
      uRim: { value: options.rim ?? 1 },
    },
    transparent: true,
    // Volumes must never occlude what is inside them — the whole point is seeing the
    // asset through the zone that protects it.
    depthWrite: false,
    blending: AdditiveBlending,
    side: DoubleSide,
  });
}
