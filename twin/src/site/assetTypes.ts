/**
 * The asset types Aetos has a bespoke 3D model for.
 *
 * `AssetType` itself is an open string — see `types/index.ts` — because a real site never
 * arrives pre-sorted into exactly these seven categories. This file is the one place that
 * distinction is made concrete: `HANDLED_ASSET_TYPES` is what `Scene.tsx` checks before
 * routing an asset to a bespoke component instead of `GenericAsset`, and `KNOWN_ASSET_TYPES`
 * is the dropdown the site editor offers, with everything else entered as free text.
 *
 * `solar_array` is handled but not listed as a Scene.tsx component target: those assets are
 * containers referenced by `SolarArrayBlock.assetId` and rendered by `SolarField`, not drawn
 * individually. It still belongs in `HANDLED_ASSET_TYPES` so it is never mistaken for an
 * unmodelled type and given a redundant generic massing box on top of the real array.
 */

export const HANDLED_ASSET_TYPES = new Set<string>([
  'substation',
  'transformer',
  'inverter',
  'control_building',
  'solar_array',
  'storage',
  'gate',
]);

export interface AssetTypeInfo {
  type: string;
  label: string;
  /** Sits in the site editor's dropdown. Extends KNOWN, not HANDLED — a curated shortlist. */
  defaultCriticality: number;
  /** Metres, used when a source (a traced polygon, an OSM way) doesn't supply a height. */
  defaultHeight: number;
}

export const KNOWN_ASSET_TYPES: AssetTypeInfo[] = [
  { type: 'substation', label: 'Substation', defaultCriticality: 10, defaultHeight: 14 },
  { type: 'transformer', label: 'Transformer', defaultCriticality: 9, defaultHeight: 7 },
  { type: 'control_building', label: 'Control building', defaultCriticality: 8, defaultHeight: 8.5 },
  { type: 'storage', label: 'Storage / battery', defaultCriticality: 7, defaultHeight: 3.2 },
  { type: 'inverter', label: 'Inverter station', defaultCriticality: 6, defaultHeight: 3.4 },
  { type: 'building', label: 'Building (generic)', defaultCriticality: 5, defaultHeight: 6 },
  { type: 'gate', label: 'Gate', defaultCriticality: 3, defaultHeight: 3 },
  { type: 'solar_array', label: 'Solar array (container)', defaultCriticality: 4, defaultHeight: 3 },
];

export function labelForAssetType(type: string): string {
  return KNOWN_ASSET_TYPES.find((t) => t.type === type)?.label ?? capitalise(type);
}

function capitalise(value: string): string {
  return value.length ? value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, ' ') : value;
}
