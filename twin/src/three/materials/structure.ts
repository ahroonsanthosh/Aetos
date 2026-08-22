/**
 * Materials for the physical site.
 *
 * A note on metalness, because it is the trap this scene fell into once already: a metallic
 * PBR surface has no diffuse response, so it can only show what it reflects. With no
 * environment map in the scene — and there is none, deliberately, since an HDRI would drag
 * a daylight look into a command display — high metalness renders as near-black no matter
 * how bright the lights are. Everything here is therefore kept close to dielectric, and the
 * metallic read comes from tight roughness and edge highlighting instead.
 *
 * The brief's constraint: a substation must still look like a substation. So the physical
 * plant is rendered as real surfaces — dark, metallic, lit — and the holographic treatment
 * is applied *on top* as edge highlighting rather than by replacing the object with a
 * glowing abstraction. Structure reads as structure; the hologram reads as the system's
 * understanding laid over it.
 *
 * Materials are shared singletons because the site draws thousands of instances of a small
 * number of surfaces, and a fresh material per mesh would mean a fresh shader program per
 * mesh.
 */

import { Color, MeshStandardMaterial } from 'three';
import { palette } from '@/lib/palette';

/** Painted steel: masts, gantries, fencing, switchgear frames. */
export const steelMaterial = new MeshStandardMaterial({
  color: new Color('#223341'),
  roughness: 0.48,
  metalness: 0.18,
});

/** Darker steel for secondary structure, so lattices do not read as a single mass. */
export const steelDarkMaterial = new MeshStandardMaterial({
  color: new Color('#17242f'),
  roughness: 0.58,
  metalness: 0.15,
});

/** Concrete: plinths, pads, building walls. */
export const concreteMaterial = new MeshStandardMaterial({
  color: new Color('#1f2a33'),
  roughness: 0.94,
  metalness: 0.02,
});

/** Transformer tanks and enclosure bodies. */
export const enclosureMaterial = new MeshStandardMaterial({
  color: new Color('#27353f'),
  roughness: 0.45,
  metalness: 0.2,
});

/**
 * The photovoltaic surface. Slightly emissive so the array field reads as active plant
 * rather than dead geometry, but well below bloom threshold — two thousand glowing panels
 * would drown every signal in the scene.
 */
export const panelMaterial = new MeshStandardMaterial({
  color: new Color('#14293f'),
  roughness: 0.24,
  metalness: 0.3,
  emissive: new Color('#11384c'),
  emissiveIntensity: 0.55,
});

/** Ceramic insulators — the one warm-ish note on the site, and a strong substation cue. */
export const insulatorMaterial = new MeshStandardMaterial({
  color: new Color('#3a424a'),
  roughness: 0.85,
  metalness: 0.05,
});

/** Road surface. */
export const roadMaterial = new MeshStandardMaterial({
  color: new Color('#0d151d'),
  roughness: 1,
  metalness: 0,
});

/**
 * Window glazing on a generic building's facade. Faintly emissive, the same reasoning as
 * `panelMaterial`: dead-flat glass at night reads as a missing texture, not as a window,
 * but it stays well under bloom threshold since a building can carry dozens of these.
 */
export const glazingMaterial = new MeshStandardMaterial({
  color: new Color('#0f2530'),
  roughness: 0.3,
  metalness: 0.1,
  emissive: new Color('#1c4552'),
  emissiveIntensity: 0.4,
});

/** Edge highlight colour used across all structures. */
export const EDGE_COLOUR = palette.holoDim;
export const EDGE_COLOUR_SELECTED = palette.holo;
