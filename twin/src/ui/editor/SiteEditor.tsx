'use client';

/**
 * Aetos Site Editor — building a site file from a real address, an uploaded image, or
 * scratch.
 *
 * Three ways in, one output: a `Site` that validates against `site/schema.ts` and either
 * downloads as a file or hands off directly into the running command view. None of the
 * three input paths is complete on its own —
 *
 *   OpenStreetMap gives real footprints, but coverage is patchy outside urban areas and a
 *   building with no tags arrives with a guessed height.
 *
 *   A traced image is only as accurate as the operator's calibration and their tracing —
 *   there is no computer vision here extracting outlines automatically, because faking
 *   that would be dishonest about what this tool actually does.
 *
 *   Manual placement is exact but slow for anything with many buildings.
 *
 * — so the intended use is combining them: import what OSM has, trace what it's missing,
 * place the rest by hand, then generate a starting sensor and zone layout and refine it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';

import type { Asset, Ground, Site } from '@/types';
import { palette } from '@/lib/palette';
import { KNOWN_ASSET_TYPES, labelForAssetType } from '@/site/assetTypes';
import { pixelToGround, type Calibration, type ImagePoint } from '@/site/import/calibration';
import { DEFAULT_IMPORT_RADIUS_M, fetchOsmSiteData } from '@/site/import/osm';
import { geocode, type GeocodeResult } from '@/site/import/geocode';
import { pointInPolygon, polygonCentroid } from '@/lib/geometry';
import { generateDefaultResources, generateDefaultSensors, generateDefaultZones } from '@/site/generate';
import { handOffSiteToCommand } from '@/site/handoff';
import { clearDraft, draftIsWorthKeeping, loadDraft, saveDraft } from '@/site/draftStore';
import { parseSiteFile, parseSiteFileText, SiteFileError, serialiseSite } from '@/site/schema';
import { TraceCanvas, type TraceMode, type TraceShape } from './TraceCanvas';
import { fitViewTo, zoomByFactor, type ViewTransform } from './viewTransform';
import styles from './editor.module.css';

function emptyDraft(): Site {
  return {
    id: `SITE-${Date.now().toString(36).toUpperCase()}`,
    name: 'New Site',
    kind: 'Site',
    // Deliberately null island until a place is chosen. The previous default was a real
    // inhabited location, so pressing Import without editing the fields returned a
    // neighbourhood of unrelated houses — the tool's single worst first impression.
    datum: { latitude: 0, longitude: 0 },
    boundary: [],
    assets: [],
    sensors: [],
    zones: [],
    resources: [],
    roads: [],
    arrays: [],
  };
}

let assetCounter = 0;
function nextAssetId(): string {
  assetCounter += 1;
  return `DRAFT-${assetCounter}`;
}

export function SiteEditor() {
  const router = useRouter();

  const [draft, setDraft] = useState<Site>(emptyDraft);
  const [mode, setMode] = useState<TraceMode>('view');
  const [view, setView] = useState<ViewTransform>({ centreE: 0, centreN: 0, metresPerPixel: 1 });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  /**
   * Ticked rows, for operations on many assets at once.
   *
   * Separate from `selectedId`, which is the one asset being edited on the canvas and in
   * the form. Conflating them would mean you could not tick fifteen buildings for deletion
   * without the canvas trying to show fifteen sets of vertex handles.
   */
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [assetFilter, setAssetFilter] = useState('');

  const [backgroundImage, setBackgroundImage] = useState<HTMLImageElement | null>(null);
  const [imageCalibration, setImageCalibration] = useState<Calibration | null>(null);
  // The image is drawn see-through so the real map underneath stays visible for aligning
  // it — but that same transparency makes the photo's own grass- or dirt-coloured edges
  // blend into similarly-coloured map tiles, which reads as "the map got duplicated" when
  // it's really just the photo's real content, softened. This lets the operator check.
  const [imageFullOpacity, setImageFullOpacity] = useState(false);

  const [placeQuery, setPlaceQuery] = useState('');
  const [placeResults, setPlaceResults] = useState<GeocodeResult[] | null>(null);
  const [located, setLocated] = useState<GeocodeResult | null>(null);
  const [osmRadius, setOsmRadius] = useState(String(DEFAULT_IMPORT_RADIUS_M));
  const [showBasemap, setShowBasemap] = useState(true);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[] | null>(null);

  const canvasHostRef = useRef<HTMLDivElement>(null);
  const [canvasSize, setCanvasSize] = useState({ width: 800, height: 600 });

  useEffect(() => {
    const host = canvasHostRef.current;
    if (!host) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      setCanvasSize({
        width: Math.max(200, Math.floor(entry.contentRect.width)),
        height: Math.max(200, Math.floor(entry.contentRect.height)),
      });
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  /**
   * Restore whatever was being edited last time.
   *
   * Runs once, on mount. Without it, going to the command view to look at a traced building
   * and coming back destroyed the work — the draft lived only in React state, and this
   * route unmounts.
   */
  useEffect(() => {
    const stored = loadDraft();
    if (!stored || !draftIsWorthKeeping(stored.site)) return;

    setDraft(stored.site);
    if (stored.site.boundary.length >= 3) {
      setView(fitViewTo(stored.site.boundary, canvasSize.width, canvasSize.height));
    }
    // A datum that is not null island means a place was chosen before; treat the site as
    // located so the basemap and the import button come back enabled.
    if (stored.site.datum.latitude !== 0 || stored.site.datum.longitude !== 0) {
      setLocated({
        label: stored.site.name,
        latitude: stored.site.datum.latitude,
        longitude: stored.site.datum.longitude,
        suggestedRadius: Number.parseFloat(osmRadius) || 400,
      });
    }
    setStatus(
      `Restored your unsaved work from ${new Date(stored.savedAt).toLocaleString()}. ` +
        'Use "Start over" to discard it.',
    );
    // Deliberately once-only: this is a restore, not a subscription. Re-running it on a
    // canvas resize would overwrite live edits with the last saved copy.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Autosave, debounced.
   *
   * A trace is a lot of clicks to lose, and there is no server to save to, so the draft is
   * written to local storage a moment after each change rather than on an explicit save the
   * operator has no reason to expect they need.
   */
  useEffect(() => {
    if (!draftIsWorthKeeping(draft)) return;
    const timer = setTimeout(() => saveDraft(draft), 400);
    return () => clearTimeout(timer);
  }, [draft]);

  // --- Derived shapes for the canvas ------------------------------------------------------

  const shapes: TraceShape[] = useMemo(
    () =>
      draft.assets.map((asset) => ({
        id: asset.id,
        polygon: asset.footprintPolygon
          ? asset.footprintPolygon.map((p) => ({ e: p.e + asset.position.e, n: p.n + asset.position.n }))
          : rectangleFor(asset),
        colour: asset.criticality >= 8 ? palette.warning : asset.criticality >= 6 ? palette.caution : palette.holo,
        emphasis: asset.criticality >= 8,
      })),
    [draft.assets],
  );

  const selectedAsset = draft.assets.find((a) => a.id === selectedId) ?? null;

  // --- Import: OpenStreetMap ---------------------------------------------------------------

  const searchPlace = async () => {
    setBusy(true);
    setStatus('Searching…');
    setPlaceResults(null);
    try {
      const results = await geocode(placeQuery);
      setPlaceResults(results);
      setStatus(
        results.length === 0
          ? 'Nothing found. Try a more specific name, or a nearby town plus the site name.'
          : `${results.length} match${results.length === 1 ? '' : 'es'} — pick one to centre the map.`,
      );
    } catch (err) {
      setStatus(`Place search failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  /**
   * Choosing a place sets the datum and moves the map there — but imports nothing. Seeing
   * where you are before pulling in buildings is the entire point of the change; the old
   * flow went straight from typed coordinates to a screenful of unrelated houses.
   */
  const choosePlace = (result: GeocodeResult) => {
    setLocated(result);
    setPlaceResults(null);
    setOsmRadius(String(result.suggestedRadius));
    setDraft((current) => ({
      ...current,
      name: current.name === 'New Site' ? result.label.split(',')[0]!.trim() : current.name,
      datum: { latitude: result.latitude, longitude: result.longitude },
    }));
    // Frame roughly the suggested radius, so the map opens showing the whole place.
    setView({
      centreE: 0,
      centreN: 0,
      metresPerPixel: (result.suggestedRadius * 2.4) / Math.max(canvasSize.height, 1),
    });
    setStatus(`Centred on ${result.label}. Draw the boundary, then import buildings inside it.`);
  };

  const importFromOsm = async () => {
    const radius = Number.parseFloat(osmRadius);
    if (!located || !Number.isFinite(radius)) {
      setStatus('Search for a place first, so the import knows where to look.');
      return;
    }

    setBusy(true);
    setStatus('Querying OpenStreetMap…');
    try {
      const result = await fetchOsmSiteData(
        { latitude: draft.datum.latitude, longitude: draft.datum.longitude },
        radius,
      );

      // If a boundary has already been drawn, only buildings inside it are the site's.
      // Without this the import is indiscriminate — every house, shed and garage within the
      // radius arrives as an asset, which is what made the first version unusable.
      const hasBoundary = draft.boundary.length >= 3;
      const kept = hasBoundary
        ? result.assets.filter((a) => pointInPolygon(a.position, draft.boundary))
        : result.assets;
      const rejected = result.assets.length - kept.length;

      setDraft((current) => ({
        ...current,
        boundary: current.boundary.length === 0 ? result.suggestedBoundary : current.boundary,
        assets: [...current.assets, ...kept],
      }));

      const frame = hasBoundary ? draft.boundary : result.suggestedBoundary;
      if (frame.length >= 3) setView(fitViewTo(frame, canvasSize.width, canvasSize.height));

      setStatus(
        kept.length === 0
          ? hasBoundary
            ? `OpenStreetMap returned ${result.assets.length} building(s), none inside your boundary. Widen the boundary or the radius.`
            : 'OpenStreetMap has no traced buildings here. Trace a reference image instead.'
          : `Imported ${kept.length} building${kept.length === 1 ? '' : 's'}` +
            (rejected ? `, ignored ${rejected} outside the boundary` : '') +
            (result.skipped ? `, skipped ${result.skipped} with no usable outline` : '') +
            '. Review and delete anything that is not yours.',
      );
    } catch (err) {
      setStatus(`OpenStreetMap import failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // --- Import: reference image -------------------------------------------------------------

  const onImageFile = (file: File) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      setBackgroundImage(img);
      setImageCalibration(null);
      setMode('calibrate');
      setStatus('Click two points on the image whose real-world distance you know.');
    };
    img.src = url;
  };

  const onCalibrated = useCallback(
    (pointA: ImagePoint, pointB: ImagePoint) => {
      const distanceText = window.prompt('Real-world distance between the two points, in metres:');
      const distance = distanceText ? Number.parseFloat(distanceText) : NaN;
      if (!Number.isFinite(distance) || distance <= 0) {
        setStatus('Calibration cancelled — no valid distance was given.');
        return;
      }
      if (!backgroundImage) return;

      const calibration: Calibration = {
        pointA,
        pointB,
        realDistanceMetres: distance,
        originPixel: { x: backgroundImage.width / 2, y: backgroundImage.height / 2 },
        // Starts pinned to the datum — right only if the screenshot happens to be centred
        // exactly on the searched point. "Align image" mode below moves it from here.
        originGround: { e: 0, n: 0 },
        northBearing: 0,
      };
      setImageCalibration(calibration);

      if (located) {
        // A real basemap is in play, so the operator may already be looking at exactly
        // where the image needs to go — recentring the camera on the image's *current*
        // (wrong, datum-pinned) position would yank them away from that spot right before
        // asking them to drag the image to it. Leave the view alone; only the site's own
        // "Fit view" button or the mode switch below moves the camera from here on.
        setMode('position-image');
        setStatus(
          'Image calibrated. Drag it onto the real site — drag empty space to look around instead. ' +
            'Trace the boundary and buildings once it lines up.',
        );
      } else {
        // No basemap to get lost in — the image itself is the whole working canvas, so
        // framing it is helpful rather than disruptive.
        const corners = [
          { x: 0, y: 0 },
          { x: backgroundImage.width, y: 0 },
          { x: backgroundImage.width, y: backgroundImage.height },
          { x: 0, y: backgroundImage.height },
        ].map((p) => pixelToGround(p, calibration));
        setView(fitViewTo(corners, canvasSize.width, canvasSize.height));
        setMode('view');
        setStatus('Image calibrated. Trace the boundary and any buildings on top of it.');
      }
    },
    [backgroundImage, canvasSize.width, canvasSize.height, located],
  );

  // --- Tracing and placing -------------------------------------------------------------------

  const onTraceComplete = (polygon: Ground[]) => {
    if (mode === 'trace-boundary') {
      setDraft((current) => ({ ...current, boundary: polygon }));
      setMode('view');
      setStatus('Boundary set.');
      return;
    }

    if (mode === 'trace-asset') {
      const centroid = polygonCentroid(polygon);
      const es = polygon.map((p) => p.e);
      const ns = polygon.map((p) => p.n);
      const asset: Asset = {
        id: nextAssetId(),
        name: `Building ${draft.assets.length + 1}`,
        type: 'building',
        position: { e: centroid.e, n: centroid.n, u: 0 },
        footprint: {
          width: Math.max(Math.max(...es) - Math.min(...es), 1),
          depth: Math.max(Math.max(...ns) - Math.min(...ns), 1),
          height: 6,
        },
        footprintPolygon: polygon.map((p) => ({ e: p.e - centroid.e, n: p.n - centroid.n })),
        heading: 0,
        criticality: 5,
        status: 'operational',
        sensorsCovering: [],
        activeThreats: [],
        protectionScore: 0,
      };
      setDraft((current) => ({ ...current, assets: [...current.assets, asset] }));
      setSelectedId(asset.id);
      setMode('view');
    }
  };

  const onPlace = (point: Ground) => {
    const asset: Asset = {
      id: nextAssetId(),
      name: `Asset ${draft.assets.length + 1}`,
      type: 'building',
      position: { e: point.e, n: point.n, u: 0 },
      footprint: { width: 8, depth: 8, height: 5 },
      heading: 0,
      criticality: 5,
      status: 'operational',
      sensorsCovering: [],
      activeThreats: [],
      protectionScore: 0,
    };
    setDraft((current) => ({ ...current, assets: [...current.assets, asset] }));
    setSelectedId(asset.id);
    setMode('view');
  };

  // --- Editing shape geometry -----------------------------------------------------------

  /**
   * Apply a change to one asset's outline, in absolute ground coordinates.
   *
   * The canvas works in absolute ground space; an asset stores its outline relative to its
   * own position. This is the one place that conversion happens, and it re-derives the
   * position and bounding footprint afterwards so the two never drift apart — a moved
   * corner has to change the asset's centre and its width and depth, or the 3D view and the
   * form would keep reporting the shape it used to be.
   */
  const updateOutline = (id: string, mutate: (absolute: Ground[]) => Ground[] | null) => {
    setDraft((current) => ({
      ...current,
      assets: current.assets.map((asset) => {
        if (asset.id !== id) return asset;

        const absolute = absoluteOutline(asset);
        const next = mutate(absolute);
        if (!next || next.length < 3) return asset;

        const centroid = polygonCentroid(next);
        const es = next.map((p) => p.e);
        const ns = next.map((p) => p.n);

        return {
          ...asset,
          position: { ...asset.position, e: centroid.e, n: centroid.n },
          footprint: {
            ...asset.footprint,
            width: Math.max(Math.max(...es) - Math.min(...es), 0.5),
            depth: Math.max(Math.max(...ns) - Math.min(...ns), 0.5),
          },
          footprintPolygon: next.map((p) => ({ e: p.e - centroid.e, n: p.n - centroid.n })),
          // A traced outline no longer sits square to anything, so a stored heading would
          // rotate it a second time when drawn.
          heading: 0,
        };
      }),
    }));
  };

  const onMoveAsset = (id: string, deltaE: number, deltaN: number) => {
    setDraft((current) => ({
      ...current,
      assets: current.assets.map((asset) =>
        asset.id === id
          ? { ...asset, position: { ...asset.position, e: asset.position.e + deltaE, n: asset.position.n + deltaN } }
          : asset,
      ),
    }));
  };

  const onMoveVertex = (id: string, index: number, point: Ground) => {
    updateOutline(id, (absolute) => absolute.map((p, i) => (i === index ? point : p)));
  };

  const onInsertVertex = (id: string, afterIndex: number, point: Ground) => {
    updateOutline(id, (absolute) => {
      const next = [...absolute];
      next.splice(afterIndex + 1, 0, point);
      return next;
    });
  };

  const onDeleteVertex = (id: string, index: number) => {
    updateOutline(id, (absolute) =>
      // Refused below three: a polygon with two corners is not a shape, and silently
      // allowing it would leave an asset that cannot be drawn or selected again.
      absolute.length <= 3 ? null : absolute.filter((_, i) => i !== index),
    );
  };

  // --- Aligning the calibrated reference image ------------------------------------------

  /**
   * Slide the calibrated image over the map. Calibration fixes scale, not position — the
   * two-point click says nothing about where on Earth the image sits — so it starts pinned
   * to the site datum, which is right only when the screenshot happens to be centred exactly
   * on the searched point. This is how the operator corrects it afterwards, without
   * re-doing the distance calibration.
   */
  const onMoveImage = (deltaE: number, deltaN: number) => {
    setImageCalibration((current) =>
      current
        ? {
            ...current,
            originGround: {
              e: (current.originGround?.e ?? 0) + deltaE,
              n: (current.originGround?.n ?? 0) + deltaN,
            },
          }
        : current,
    );
  };

  const resetImagePosition = () => {
    setImageCalibration((current) => (current ? { ...current, originGround: { e: 0, n: 0 } } : current));
  };

  /**
   * Jump the camera to frame the image, on request. Calibration itself no longer does this
   * automatically — snapping the view to the image's position right after calibrating
   * yanked the operator away from wherever they had already navigated to on the real map,
   * which is exactly the wrong moment to move the camera when they are about to drag the
   * image to that same spot.
   */
  const centreViewOnImage = () => {
    if (!backgroundImage || !imageCalibration) return;
    const corners = [
      { x: 0, y: 0 },
      { x: backgroundImage.width, y: 0 },
      { x: backgroundImage.width, y: backgroundImage.height },
      { x: 0, y: backgroundImage.height },
    ].map((p) => pixelToGround(p, imageCalibration));
    setView(fitViewTo(corners, canvasSize.width, canvasSize.height));
  };

  /**
   * Rotate the image about its own centre. A satellite screenshot is essentially never
   * north-up, and translating it can only fix position — this is the other degree of
   * freedom calibration leaves as a guess (`northBearing` defaults to 0).
   */
  const setImageRotationDegrees = (degrees: number) => {
    if (!Number.isFinite(degrees)) return;
    setImageCalibration((current) => (current ? { ...current, northBearing: (degrees * Math.PI) / 180 } : current));
  };

  const nudgeImageRotation = (deltaDegrees: number) => {
    setImageCalibration((current) =>
      current ? { ...current, northBearing: current.northBearing + (deltaDegrees * Math.PI) / 180 } : current,
    );
  };

  // --- Editing the selected asset -----------------------------------------------------------

  const updateSelected = (patch: Partial<Asset>) => {
    if (!selectedId) return;
    setDraft((current) => ({
      ...current,
      assets: current.assets.map((a) => (a.id === selectedId ? { ...a, ...patch } : a)),
    }));
  };

  const deleteSelected = () => {
    if (!selectedId) return;
    setDraft((current) => ({ ...current, assets: current.assets.filter((a) => a.id !== selectedId) }));
    setSelectedId(null);
  };

  // --- Bulk asset operations --------------------------------------------------------------

  /**
   * An OSM import arrives as tens of identically-named buildings at a default criticality.
   * Correcting those one at a time through the single-asset form was the slowest part of
   * building a real site, so everything below works on the ticked set.
   */
  const visibleAssets = useMemo(() => {
    const needle = assetFilter.trim().toLowerCase();
    if (!needle) return draft.assets;
    return draft.assets.filter(
      (a) => a.name.toLowerCase().includes(needle) || a.type.toLowerCase().includes(needle),
    );
  }, [draft.assets, assetFilter]);

  const toggleChecked = (id: string) => {
    setCheckedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  /** Ticks or clears everything currently listed — the filter, not the whole site. */
  const toggleAllVisible = () => {
    const visibleIds = visibleAssets.map((a) => a.id);
    const allTicked = visibleIds.length > 0 && visibleIds.every((id) => checkedIds.has(id));
    setCheckedIds((current) => {
      const next = new Set(current);
      for (const id of visibleIds) {
        if (allTicked) next.delete(id);
        else next.add(id);
      }
      return next;
    });
  };

  const bulkPatch = (patch: Partial<Asset>) => {
    if (checkedIds.size === 0) return;
    setDraft((current) => ({
      ...current,
      assets: current.assets.map((a) => (checkedIds.has(a.id) ? { ...a, ...patch } : a)),
    }));
  };

  const bulkDelete = () => {
    if (checkedIds.size === 0) return;
    if (!window.confirm(`Delete ${checkedIds.size} asset${checkedIds.size === 1 ? '' : 's'}?`)) return;

    setDraft((current) => ({
      ...current,
      assets: current.assets.filter((a) => !checkedIds.has(a.id)),
    }));
    if (selectedId && checkedIds.has(selectedId)) setSelectedId(null);
    setStatus(`Deleted ${checkedIds.size} asset${checkedIds.size === 1 ? '' : 's'}.`);
    setCheckedIds(new Set());
  };

  // --- Defaults, export, preview -------------------------------------------------------------

  const generateDefaults = () => {
    const { zones, assignments } = generateDefaultZones(draft);
    setDraft((current) => ({
      ...current,
      zones,
      sensors: generateDefaultSensors(current.boundary),
      resources: generateDefaultResources(current.boundary),
      assets: current.assets.map((a) =>
        assignments[a.id] ? { ...a, protectionZoneId: assignments[a.id] } : a,
      ),
    }));
    setStatus('Generated a starting sensor layout and protected zones — review before relying on it.');
  };

  const validated = useCallback((): Site | null => {
    try {
      const site = parseSiteFile(JSON.parse(serialiseSite(draft)));
      setErrors(null);
      return site;
    } catch (err) {
      setErrors(err instanceof SiteFileError ? err.issues.map((i) => `${i.path} — ${i.message}`) : [String(err)]);
      return null;
    }
  }, [draft]);

  const exportJson = () => {
    const site = validated();
    if (!site) return;
    const blob = new Blob([serialiseSite(site)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${site.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const previewInCommand = () => {
    const site = validated();
    if (!site) return;
    handOffSiteToCommand(serialiseSite(site));
    router.push('/');
  };

  const startOver = () => {
    if (draftIsWorthKeeping(draft) && !window.confirm('Discard the current site and start over?')) {
      return;
    }
    clearDraft();
    setDraft(emptyDraft());
    setLocated(null);
    setPlaceResults(null);
    setPlaceQuery('');
    setSelectedId(null);
    setBackgroundImage(null);
    setImageCalibration(null);
    setErrors(null);
    setView({ centreE: 0, centreN: 0, metresPerPixel: 1 });
    setStatus('Cleared. Search for a site to begin.');
  };

  const onLoadFile = async (file: File) => {
    try {
      const text = await file.text();
      const site = parseSiteFileText(text);
      setDraft(site);
      setErrors(null);
      setSelectedId(null);
      setView(fitViewTo(site.boundary.length ? site.boundary : [{ e: 0, n: 0 }], canvasSize.width, canvasSize.height));
      setStatus(`Loaded ${site.name}.`);
    } catch (err) {
      setErrors(err instanceof SiteFileError ? err.issues.map((i) => `${i.path} — ${i.message}`) : [String(err)]);
    }
  };

  return (
    <div className={styles.root}>
      <header className={styles.header}>
        <div className={styles.wordmark}>
          AETOS <span className={styles.wordmarkThin}>SITE EDITOR</span>
        </div>
        <input
          className={styles.nameInput}
          value={draft.name}
          onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
        />
        <a className={styles.headerLink} href="/">
          ← Command view
        </a>
      </header>

      <div className={styles.body}>
        <aside className={styles.left}>
          <Section title="1 · Find the site">
            <p className={styles.hint}>
              Search by name or address — you should not need to know its coordinates. Powered
              by OpenStreetMap&apos;s geocoder, not Google.
            </p>
            <div className={styles.searchRow}>
              <input
                value={placeQuery}
                placeholder="e.g. Moneypoint Power Station, Clare"
                onChange={(e) => setPlaceQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void searchPlace();
                }}
              />
              <button className={styles.button} disabled={busy || !placeQuery.trim()} onClick={() => void searchPlace()}>
                Search
              </button>
            </div>

            {placeResults && placeResults.length > 0 && (
              <ul className={styles.resultList}>
                {placeResults.map((r, i) => (
                  <li key={i}>
                    <button className={styles.resultRow} onClick={() => choosePlace(r)}>
                      {r.label}
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {located && (
              <p className={styles.located}>
                📍 {located.label}
                <br />
                <span className={styles.hint}>
                  {draft.datum.latitude.toFixed(5)}, {draft.datum.longitude.toFixed(5)}
                </span>
              </p>
            )}

            <label className={styles.checkRow}>
              <input
                type="checkbox"
                checked={showBasemap}
                onChange={(e) => setShowBasemap(e.target.checked)}
              />
              Show map underneath
            </label>
          </Section>

          <Section title="2 · Import buildings">
            <p className={styles.hint}>
              Draw the boundary first (toolbar above) and only buildings inside it are
              imported. Without a boundary you get everything within the radius, which on a
              populated map means a lot of houses.
            </p>
            <Field label="Search radius (m)">
              <input value={osmRadius} onChange={(e) => setOsmRadius(e.target.value)} />
            </Field>
            <button
              className={styles.primaryButton}
              disabled={busy || !located}
              onClick={() => void importFromOsm()}
            >
              Import from OpenStreetMap
            </button>
            {!located && <p className={styles.hint}>Find a site first.</p>}
            <p className={styles.hint}>
              Coverage is patchy outside towns. If nothing comes back, trace a reference
              image instead.
            </p>
          </Section>

          <Section title="Trace a reference image">
            <p className={styles.hint}>
              A satellite screenshot or site plan. You calibrate its scale by clicking two
              points of known distance, then trace outlines on top. No automatic building
              extraction — this reads what you draw.
            </p>
            <input
              type="file"
              accept="image/*"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) onImageFile(file);
                e.target.value = '';
              }}
            />
            {backgroundImage && !imageCalibration && mode !== 'calibrate' && (
              <button className={styles.button} onClick={() => setMode('calibrate')}>
                Re-calibrate image
              </button>
            )}
            {backgroundImage && imageCalibration && (
              <>
                <p className={styles.hint}>
                  Calibration fixes scale, not position — it starts centred on the searched
                  point, which is rarely where the screenshot actually is. In &quot;Align
                  image&quot; mode, drag the image itself to move it, or drag empty space to
                  pan the map underneath it.
                </p>
                <p className={styles.hint}>
                  Offset from the search point: {(imageCalibration.originGround?.e ?? 0).toFixed(1)} m
                  east, {(imageCalibration.originGround?.n ?? 0).toFixed(1)} m north.
                </p>
                <div className={styles.searchRow}>
                  <button className={styles.linkButton} onClick={resetImagePosition}>
                    Reset image position
                  </button>
                  <button className={styles.linkButton} onClick={centreViewOnImage}>
                    Centre view on image
                  </button>
                </div>
                <Field label="Rotation (° clockwise) — a screenshot is rarely north-up">
                  <input
                    type="number"
                    step={1}
                    value={Math.round((imageCalibration.northBearing * 180) / Math.PI)}
                    onChange={(e) => setImageRotationDegrees(Number(e.target.value))}
                  />
                </Field>
                <div className={styles.searchRow}>
                  <button className={styles.button} onClick={() => nudgeImageRotation(-5)}>
                    −5°
                  </button>
                  <button className={styles.button} onClick={() => nudgeImageRotation(5)}>
                    +5°
                  </button>
                  <button className={styles.button} onClick={() => nudgeImageRotation(-90)}>
                    Rotate ↺ 90°
                  </button>
                  <button className={styles.button} onClick={() => nudgeImageRotation(90)}>
                    Rotate ↻ 90°
                  </button>
                </div>
                <p className={styles.hint}>
                  If it is the wrong size rather than the wrong place or angle, re-calibrate
                  instead — scale comes from the two-point distance, not from dragging.
                </p>
                <label className={styles.checkRow}>
                  <input
                    type="checkbox"
                    checked={imageFullOpacity}
                    onChange={(e) => setImageFullOpacity(e.target.checked)}
                  />
                  Show image at full opacity
                </label>
                <p className={styles.hint}>
                  The image is normally see-through so the real map stays visible underneath
                  it for aligning — which also means its own grass- or dirt-coloured edges
                  blend into similarly-coloured map tiles. Tick this to check what is really
                  in the photo versus what the map is showing through it.
                </p>
              </>
            )}
          </Section>

          <Section title="Load / continue editing">
            <input
              type="file"
              accept="application/json,.json"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void onLoadFile(file);
                e.target.value = '';
              }}
            />
          </Section>
        </aside>

        <div className={styles.centre}>
          <div className={styles.toolbar}>
            <ToolButton active={mode === 'view'} onClick={() => setMode('view')}>
              Select
            </ToolButton>
            <ToolButton active={mode === 'trace-boundary'} onClick={() => setMode('trace-boundary')}>
              Trace boundary
            </ToolButton>
            <ToolButton active={mode === 'trace-asset'} onClick={() => setMode('trace-asset')}>
              Trace building
            </ToolButton>
            <ToolButton active={mode === 'place-asset'} onClick={() => setMode('place-asset')}>
              Place asset
            </ToolButton>
            {backgroundImage && (
              <ToolButton active={mode === 'calibrate'} onClick={() => setMode('calibrate')}>
                Calibrate image
              </ToolButton>
            )}
            {backgroundImage && imageCalibration && (
              <ToolButton active={mode === 'position-image'} onClick={() => setMode('position-image')}>
                Align image
              </ToolButton>
            )}
            {backgroundImage && imageCalibration && (
              <span className={styles.toolbarRotate} title="Rotate the reference image">
                <span className={styles.toolbarRotateLabel}>Rotate</span>
                <input
                  type="number"
                  className={styles.toolbarRotateInput}
                  value={Math.round((imageCalibration.northBearing * 180) / Math.PI)}
                  onChange={(e) => setImageRotationDegrees(Number(e.target.value))}
                />
                <span className={styles.toolbarRotateLabel}>°</span>
              </span>
            )}
            <span className={styles.toolbarSpacer} />
            <span className={styles.zoomHint}>
              {mode === 'position-image'
                ? 'Drag to move the image · right-drag (or middle-drag) to look around instead'
                : mode === 'view' && selectedId
                  ? 'Drag the shape or its corners · alt-click a corner to remove it'
                  : 'Drag to pan · scroll to zoom'}
            </span>
            <button
              className={styles.tool}
              title="Zoom out"
              onClick={() => setView((v) => zoomByFactor(v, 1.4))}
            >
              −
            </button>
            <button
              className={styles.tool}
              title="Zoom in"
              onClick={() => setView((v) => zoomByFactor(v, 1 / 1.4))}
            >
              +
            </button>
            <button
              className={styles.button}
              onClick={() =>
                setView(
                  fitViewTo(
                    draft.boundary.length ? draft.boundary : draft.assets.map((a) => a.position),
                    canvasSize.width,
                    canvasSize.height,
                  ),
                )
              }
            >
              Fit view
            </button>
          </div>

          <div className={styles.canvasHost} ref={canvasHostRef}>
            <TraceCanvas
              width={canvasSize.width}
              height={canvasSize.height}
              view={view}
              onViewChange={setView}
              mode={mode}
              backgroundImage={backgroundImage}
              imageCalibration={imageCalibration}
              imageFullOpacity={imageFullOpacity}
              onCalibrated={onCalibrated}
              boundary={draft.boundary}
              shapes={shapes}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onTraceComplete={onTraceComplete}
              onPlace={onPlace}
              datum={draft.datum}
              showBasemap={showBasemap && located !== null}
              onMoveAsset={onMoveAsset}
              onMoveVertex={onMoveVertex}
              onInsertVertex={onInsertVertex}
              onDeleteVertex={onDeleteVertex}
              onMoveImage={onMoveImage}
            />
          </div>

          {status && <div className={styles.status}>{status}</div>}
          {errors && (
            <div className={styles.errors}>
              {errors.map((e, i) => (
                <div key={i}>{e}</div>
              ))}
            </div>
          )}
        </div>

        <aside className={styles.right}>
          {selectedAsset ? (
            <AssetForm
              asset={selectedAsset}
              onChange={updateSelected}
              onDelete={deleteSelected}
              onClose={() => setSelectedId(null)}
            />
          ) : (
            <Section title={`Assets (${draft.assets.length})`}>
              {draft.assets.length === 0 ? (
                <p className={styles.hint}>
                  Import from OpenStreetMap, trace an image, or use &quot;Place asset&quot; above.
                </p>
              ) : (
                <>
                  {draft.assets.length > 6 && (
                    <input
                      className={styles.filterInput}
                      value={assetFilter}
                      placeholder="Filter by name or type…"
                      onChange={(e) => setAssetFilter(e.target.value)}
                    />
                  )}

                  <div className={styles.listHeader}>
                    <label className={styles.checkRow}>
                      <input
                        type="checkbox"
                        checked={
                          visibleAssets.length > 0 &&
                          visibleAssets.every((a) => checkedIds.has(a.id))
                        }
                        onChange={toggleAllVisible}
                      />
                      Select all{assetFilter ? ' shown' : ''}
                    </label>
                    {checkedIds.size > 0 && (
                      <span className={styles.checkedCount}>{checkedIds.size} ticked</span>
                    )}
                  </div>

                  {checkedIds.size > 0 && (
                    <div className={styles.bulkBar}>
                      <label className={styles.field}>
                        <span>Set type for {checkedIds.size}</span>
                        <input
                          list="asset-type-options"
                          placeholder="e.g. substation"
                          onKeyDown={(e) => {
                            if (e.key !== 'Enter') return;
                            const value = (e.target as HTMLInputElement).value.trim();
                            if (value) bulkPatch({ type: value });
                            (e.target as HTMLInputElement).value = '';
                          }}
                        />
                      </label>
                      <label className={styles.field}>
                        <span>Set criticality</span>
                        <select
                          className={styles.select}
                          defaultValue=""
                          onChange={(e) => {
                            const value = Number(e.target.value);
                            if (value) bulkPatch({ criticality: value as Asset['criticality'] });
                            e.target.value = '';
                          }}
                        >
                          <option value="">Choose…</option>
                          {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => (
                            <option key={n} value={n}>
                              {n}
                            </option>
                          ))}
                        </select>
                      </label>
                      <button className={styles.dangerButton} onClick={bulkDelete}>
                        Delete {checkedIds.size} asset{checkedIds.size === 1 ? '' : 's'}
                      </button>
                    </div>
                  )}

                  <ul className={styles.assetList}>
                    {visibleAssets.map((a) => (
                      <li key={a.id} className={styles.assetItem}>
                        <input
                          type="checkbox"
                          checked={checkedIds.has(a.id)}
                          onChange={() => toggleChecked(a.id)}
                          aria-label={`Select ${a.name}`}
                        />
                        <button
                          className={`${styles.assetRow} ${a.id === selectedId ? styles.assetRowSelected : ''}`}
                          onClick={() => setSelectedId(a.id)}
                        >
                          <span>{a.name}</span>
                          <span className={styles.assetType}>
                            {labelForAssetType(a.type)} · {a.criticality}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>

                  {visibleAssets.length === 0 && (
                    <p className={styles.hint}>Nothing matches that filter.</p>
                  )}

                  <datalist id="asset-type-options">
                    {KNOWN_ASSET_TYPES.map((t) => (
                      <option key={t.type} value={t.type} />
                    ))}
                  </datalist>
                </>
              )}
            </Section>
          )}

          <Section title="Sensors, zones & resources">
            <p className={styles.hint}>
              One click generates a starting layout: sensors spaced around the boundary, a
              perimeter zone, and a protected volume around anything criticality 6 or above.
              Review it — this is a starting point, not a design.
            </p>
            <button className={styles.button} onClick={generateDefaults} disabled={draft.boundary.length < 3}>
              (Re)generate sensors, zones &amp; resources
            </button>
            <p className={styles.hint}>
              {draft.sensors.length} sensor{draft.sensors.length === 1 ? '' : 's'} ·{' '}
              {draft.zones.length} zone{draft.zones.length === 1 ? '' : 's'} ·{' '}
              {draft.resources.length} resource{draft.resources.length === 1 ? '' : 's'}
            </p>
          </Section>
        </aside>
      </div>

      <footer className={styles.footer}>
        <span className={styles.autosaveNote}>
          Your work is saved in this browser automatically.
        </span>
        <button className={styles.button} onClick={startOver}>
          Start over
        </button>
        <button className={styles.button} onClick={exportJson}>
          Download site.json
        </button>
        <button className={styles.primaryButton} onClick={previewInCommand}>
          Preview in Command
        </button>
      </footer>
    </div>
  );
}

// ---------------------------------------------------------------------------

/**
 * An asset's outline in absolute ground coordinates.
 *
 * An asset placed rather than traced has no `footprintPolygon`, only a width and depth. Its
 * corners are derived from that box here, so dragging one converts it into a real traced
 * outline on the spot — the operator does not have to know which kind of asset they have
 * before they can adjust its shape.
 */
function absoluteOutline(asset: Asset): Ground[] {
  if (asset.footprintPolygon && asset.footprintPolygon.length >= 3) {
    return asset.footprintPolygon.map((p) => ({
      e: p.e + asset.position.e,
      n: p.n + asset.position.n,
    }));
  }
  return rectangleFor(asset);
}

function rectangleFor(asset: Asset): Ground[] {
  const hw = asset.footprint.width / 2;
  const hd = asset.footprint.depth / 2;
  const { e, n } = asset.position;
  return [
    { e: e - hw, n: n - hd },
    { e: e + hw, n: n - hd },
    { e: e + hw, n: n + hd },
    { e: e - hw, n: n + hd },
  ];
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className={styles.section}>
      <h3 className={styles.sectionTitle}>{title}</h3>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className={styles.field}>
      <span>{label}</span>
      {children}
    </label>
  );
}

function ToolButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button className={`${styles.tool} ${active ? styles.toolActive : ''}`} onClick={onClick}>
      {children}
    </button>
  );
}

function AssetForm({
  asset,
  onChange,
  onDelete,
  onClose,
}: {
  asset: Asset;
  onChange: (patch: Partial<Asset>) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  return (
    <Section title="Selected asset">
      <button className={styles.linkButton} onClick={onClose}>
        ← Back to asset list
      </button>
      <Field label="Name">
        <input value={asset.name} onChange={(e) => onChange({ name: e.target.value })} />
      </Field>
      <Field label="Type">
        <input
          list="asset-type-options"
          value={asset.type}
          onChange={(e) => onChange({ type: e.target.value })}
        />
        <datalist id="asset-type-options">
          {KNOWN_ASSET_TYPES.map((t) => (
            <option key={t.type} value={t.type} />
          ))}
        </datalist>
      </Field>
      <Field label="Criticality (1–10)">
        <input
          type="number"
          min={1}
          max={10}
          value={asset.criticality}
          onChange={(e) => onChange({ criticality: clampCriticality(Number(e.target.value)) })}
        />
      </Field>
      <Field label="Height (m)">
        <input
          type="number"
          min={0.5}
          step={0.5}
          value={asset.footprint.height}
          onChange={(e) => onChange({ footprint: { ...asset.footprint, height: Number(e.target.value) } })}
        />
      </Field>
      <Field label="Width (m)">
        <input
          type="number"
          min={0.5}
          step={0.5}
          value={asset.footprint.width}
          onChange={(e) => onChange({ footprint: { ...asset.footprint, width: Number(e.target.value) } })}
        />
      </Field>
      <Field label="Depth (m)">
        <input
          type="number"
          min={0.5}
          step={0.5}
          value={asset.footprint.depth}
          onChange={(e) => onChange({ footprint: { ...asset.footprint, depth: Number(e.target.value) } })}
        />
      </Field>
      {asset.footprintPolygon && (
        <p className={styles.hint}>
          Has a traced outline.{' '}
          <button className={styles.linkButton} onClick={() => onChange({ footprintPolygon: undefined })}>
            Clear it, use a plain box instead
          </button>
        </p>
      )}
      <button className={styles.dangerButton} onClick={onDelete}>
        Delete asset
      </button>
    </Section>
  );
}

function clampCriticality(value: number): Asset['criticality'] {
  const clamped = Math.min(10, Math.max(1, Math.round(Number.isFinite(value) ? value : 5)));
  return clamped as Asset['criticality'];
}
