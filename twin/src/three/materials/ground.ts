/**
 * The ground plane.
 *
 * A procedural grid rather than a texture, for two reasons: it stays crisp at every zoom
 * level the camera allows, and the line width can be held constant in screen space using
 * derivatives, so the grid never aliases into moiré when the camera pulls back — which is
 * exactly when a textured grid falls apart and starts looking cheap.
 *
 * The grid is a reference frame, not decoration. It is dim enough to read as the surface
 * the site sits on, and it fades out with distance so the site has a horizon instead of a
 * hard edge.
 */

import { Color, DoubleSide, ShaderMaterial } from 'three';

const vertexShader = /* glsl */ `
  varying vec2 vPlane;
  varying float vDist;

  void main() {
    vec4 world = modelMatrix * vec4(position, 1.0);
    // The plane is built in XY and rotated flat, so local xy is the ground plane.
    vPlane = position.xy;
    vDist = length(world.xz);
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

const fragmentShader = /* glsl */ `
  varying vec2 vPlane;
  varying float vDist;

  uniform vec3  uGround;
  uniform vec3  uGrid;
  uniform vec3  uGridMajor;
  uniform float uMinor;      // minor cell size, metres
  uniform float uMajor;      // major cell size, metres
  uniform float uFadeStart;
  uniform float uFadeEnd;

  // Screen-space-constant grid lines. fwidth keeps the line one pixel wide however far
  // away the fragment is, which is what stops the grid aliasing at range.
  float gridLine(vec2 p, float cell) {
    vec2 c = abs(fract(p / cell - 0.5) - 0.5) / fwidth(p / cell);
    return 1.0 - min(min(c.x, c.y), 1.0);
  }

  void main() {
    float minor = gridLine(vPlane, uMinor);
    float major = gridLine(vPlane, uMajor);

    float fade = 1.0 - smoothstep(uFadeStart, uFadeEnd, vDist);

    vec3 rgb = uGround;
    rgb = mix(rgb, uGrid,      minor * 0.8 * fade);
    rgb = mix(rgb, uGridMajor, major * 0.9 * fade);

    // A faint pool of light around the datum, so the site sits in something rather than
    // floating in a void.
    rgb += uGrid * 0.09 * (1.0 - smoothstep(0.0, 1100.0, vDist));

    gl_FragColor = vec4(rgb, 1.0);
  }
`;

export interface GroundMaterialOptions {
  ground: string;
  grid: string;
  gridMajor: string;
  minor?: number;
  major?: number;
  fadeStart?: number;
  fadeEnd?: number;
}

export function createGroundMaterial(options: GroundMaterialOptions): ShaderMaterial {
  return new ShaderMaterial({
    vertexShader,
    fragmentShader,
    uniforms: {
      uGround: { value: new Color(options.ground) },
      uGrid: { value: new Color(options.grid) },
      uGridMajor: { value: new Color(options.gridMajor) },
      uMinor: { value: options.minor ?? 25 },
      uMajor: { value: options.major ?? 250 },
      uFadeStart: { value: options.fadeStart ?? 700 },
      uFadeEnd: { value: options.fadeEnd ?? 2100 },
    },
    side: DoubleSide,
  });
}
