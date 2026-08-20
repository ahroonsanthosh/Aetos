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
import { polygonCentroid } from '@/lib/geometry';
import { palette } from '@/lib/palette';
import { KNOWN_ASSET_TYPES, labelForAssetType } from '@/site/assetTypes';
import { pixelToGround, type Calibration, type ImagePoint } from '@/site/import/calibration';
import { DEFAULT_IMPORT_RADIUS_M, fetchOsmSiteData } from '@/site/import/osm';
import { generateDefaultResources, generateDefaultSensors, generateDefaultZones } from '@/site/generate';
import { handOffSiteToCommand } from '@/site/handoff';
import { parseSiteFile, parseSiteFileText, SiteFileError, serialiseSite } from '@/site/schema';
import { TraceCanvas, type TraceMode, type TraceShape } from './TraceCanvas';
import { fitViewTo, type ViewTransform } from './viewTransform';
import styles from './editor.module.css';

function emptyDraft(): Site {
  return {
    id: `SITE-${Date.now().toString(36).toUpperCase()}`,
    name: 'New Site',
    kind: 'Site',
    datum: { latitude: 51.8985, longitude: -8.4756 },
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

  const [backgroundImage, setBackgroundImage] = useState<HTMLImageElement | null>(null);
  const [imageCalibration, setImageCalibration] = useState<Calibration | null>(null);

  const [osmLat, setOsmLat] = useState(String(draft.datum.latitude));
  const [osmLon, setOsmLon] = useState(String(draft.datum.longitude));
  const [osmRadius, setOsmRadius] = useState(String(DEFAULT_IMPORT_RADIUS_M));
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

  const importFromOsm = async () => {
    const latitude = Number.parseFloat(osmLat);
    const longitude = Number.parseFloat(osmLon);
    const radius = Number.parseFloat(osmRadius);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude) || !Number.isFinite(radius)) {
      setStatus('Latitude, longitude and radius must all be numbers.');
      return;
    }

    setBusy(true);
    setStatus('Querying OpenStreetMap…');
    try {
      const result = await fetchOsmSiteData({ latitude, longitude }, radius);
      setDraft((current) => ({
        ...current,
        datum: current.assets.length === 0 && current.boundary.length === 0
          ? { latitude, longitude }
          : current.datum,
        boundary: current.boundary.length === 0 ? result.suggestedBoundary : current.boundary,
        assets: [...current.assets, ...result.assets],
      }));
      setView(fitViewTo(result.suggestedBoundary.length ? result.suggestedBoundary : [{ e: 0, n: 0 }], canvasSize.width, canvasSize.height));
      setStatus(
        `Imported ${result.assets.length} building${result.assets.length === 1 ? '' : 's'}` +
          (result.skipped ? `, skipped ${result.skipped} with no usable outline.` : '.'),
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
        northBearing: 0,
      };
      setImageCalibration(calibration);
      setMode('view');

      const corners = [
        { x: 0, y: 0 },
        { x: backgroundImage.width, y: 0 },
        { x: backgroundImage.width, y: backgroundImage.height },
        { x: 0, y: backgroundImage.height },
      ].map((p) => pixelToGround(p, calibration));
      setView(fitViewTo(corners, canvasSize.width, canvasSize.height));
      setStatus('Image calibrated. Trace the boundary and any buildings on top of it.');
    },
    [backgroundImage, canvasSize.width, canvasSize.height],
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
          <Section title="Start from OpenStreetMap">
            <p className={styles.hint}>
              Free, keyless, real building footprints — where a mapper has traced them.
              Coverage is patchy outside urban areas.
            </p>
            <Field label="Latitude">
              <input value={osmLat} onChange={(e) => setOsmLat(e.target.value)} />
            </Field>
            <Field label="Longitude">
              <input value={osmLon} onChange={(e) => setOsmLon(e.target.value)} />
            </Field>
            <Field label="Radius (m)">
              <input value={osmRadius} onChange={(e) => setOsmRadius(e.target.value)} />
            </Field>
            <button className={styles.primaryButton} disabled={busy} onClick={() => void importFromOsm()}>
              Import from OpenStreetMap
            </button>
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
            <span className={styles.toolbarSpacer} />
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
              onCalibrated={onCalibrated}
              boundary={draft.boundary}
              shapes={shapes}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onTraceComplete={onTraceComplete}
              onPlace={onPlace}
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
            <AssetForm asset={selectedAsset} onChange={updateSelected} onDelete={deleteSelected} />
          ) : (
            <Section title={`Assets (${draft.assets.length})`}>
              {draft.assets.length === 0 ? (
                <p className={styles.hint}>
                  Import from OpenStreetMap, trace an image, or use "Place asset" above.
                </p>
              ) : (
                <ul className={styles.assetList}>
                  {draft.assets.map((a) => (
                    <li key={a.id}>
                      <button className={styles.assetRow} onClick={() => setSelectedId(a.id)}>
                        <span>{a.name}</span>
                        <span className={styles.assetType}>{labelForAssetType(a.type)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
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
}: {
  asset: Asset;
  onChange: (patch: Partial<Asset>) => void;
  onDelete: () => void;
}) {
  return (
    <Section title="Selected asset">
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
