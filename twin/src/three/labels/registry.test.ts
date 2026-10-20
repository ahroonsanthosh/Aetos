/**
 * Label placement tests.
 *
 * The pass is pure geometry over a camera projection, so it runs in node without a DOM: the
 * elements are stubs carrying a measured size and a style object, which is the entire
 * surface the arbiter touches.
 *
 * What is pinned here is the contract the substation depends on — when labels compete for
 * the same pixels, the one that matters survives, and it is always the same one.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { PerspectiveCamera } from 'three';

import { acquireLabel, declutter, LabelPriority, resetLabels } from './registry';

const WIDTH = 1920;
const HEIGHT = 1080;

/** Acquire and enter a label into the pass, as `useLabel` does on mount. */
function label(priority: number) {
  const handle = acquireLabel(priority);
  handle.attach();
  return handle;
}

/** Minimal stand-in for the label element: a measured box and a style object. */
function stubElement(width = 140, height = 34) {
  return {
    offsetWidth: width,
    offsetHeight: height,
    style: { opacity: '', pointerEvents: '' },
  } as unknown as HTMLElement;
}

function camera(): PerspectiveCamera {
  const c = new PerspectiveCamera(42, WIDTH / HEIGHT, 1, 12000);
  c.position.set(0, 400, 800);
  c.lookAt(0, 0, 0);
  c.updateMatrixWorld(true);
  c.updateProjectionMatrix();
  return c;
}

function visible(element: HTMLElement): boolean {
  // Untouched means never hidden — the pass writes only on change.
  return element.style.opacity !== '0';
}

beforeEach(() => {
  resetLabels();
});

describe('declutter', () => {
  it('keeps the higher-priority label when two occupy the same pixels', () => {
    const important = label(LabelPriority.TRACK_HIGH);
    const minor = label(LabelPriority.ASSET_BASE + 40);

    const importantEl = stubElement();
    const minorEl = stubElement();
    important.setElement(importantEl);
    minor.setElement(minorEl);

    // Same world point, so their projected boxes are identical.
    important.world.set(0, 50, 0);
    minor.world.set(0, 50, 0);

    declutter(camera(), WIDTH, HEIGHT);

    expect(visible(importantEl)).toBe(true);
    expect(visible(minorEl)).toBe(false);
  });

  it('does not care which order the labels were registered in', () => {
    // Registered lowest-first, which is the order React mount order could easily produce.
    const minor = label(LabelPriority.ASSET_BASE + 40);
    const important = label(LabelPriority.TRACK_CRITICAL);

    const minorEl = stubElement();
    const importantEl = stubElement();
    minor.setElement(minorEl);
    important.setElement(importantEl);
    minor.world.set(0, 50, 0);
    important.world.set(0, 50, 0);

    declutter(camera(), WIDTH, HEIGHT);

    expect(visible(importantEl)).toBe(true);
    expect(visible(minorEl)).toBe(false);
  });

  it('shows both when they are far enough apart on screen', () => {
    const a = label(LabelPriority.TRACK_HIGH);
    const b = label(LabelPriority.ASSET_BASE + 40);

    const aEl = stubElement();
    const bEl = stubElement();
    a.setElement(aEl);
    b.setElement(bEl);

    a.world.set(-300, 50, 0);
    b.world.set(300, 50, 0);

    declutter(camera(), WIDTH, HEIGHT);

    expect(visible(aEl)).toBe(true);
    expect(visible(bEl)).toBe(true);
  });

  it('hides a label whose anchor is behind the camera', () => {
    const behind = label(LabelPriority.TRACK_HIGH);
    const element = stubElement();
    behind.setElement(element);
    // The camera sits at z = 800 looking toward the origin, so this is well behind it.
    behind.world.set(0, 50, 3000);

    declutter(camera(), WIDTH, HEIGHT);

    expect(visible(element)).toBe(false);
  });

  it('lets an off-screen label yield its space rather than block an on-screen one', () => {
    // High priority but way off to the side; low priority but centred.
    const offscreen = label(LabelPriority.SELECTED);
    const onscreen = label(LabelPriority.ASSET_BASE + 40);

    const offscreenEl = stubElement();
    const onscreenEl = stubElement();
    offscreen.setElement(offscreenEl);
    onscreen.setElement(onscreenEl);

    offscreen.world.set(-40_000, 50, 0);
    onscreen.world.set(0, 50, 0);

    declutter(camera(), WIDTH, HEIGHT);

    expect(visible(offscreenEl)).toBe(false);
    expect(visible(onscreenEl)).toBe(true);
  });

  it('restores a label once the collision clears', () => {
    const important = label(LabelPriority.TRACK_HIGH);
    const minor = label(LabelPriority.ASSET_BASE + 40);

    const importantEl = stubElement();
    const minorEl = stubElement();
    important.setElement(importantEl);
    minor.setElement(minorEl);

    important.world.set(0, 50, 0);
    minor.world.set(0, 50, 0);
    const view = camera();

    declutter(view, WIDTH, HEIGHT);
    expect(visible(minorEl)).toBe(false);

    // The camera moves, or the track does; either way they no longer overlap.
    minor.world.set(400, 50, 0);
    declutter(view, WIDTH, HEIGHT);
    expect(visible(minorEl)).toBe(true);
  });

  it('releases a label so it stops competing once its owner unmounts', () => {
    const gone = label(LabelPriority.SELECTED);
    const remaining = label(LabelPriority.ASSET_BASE + 40);

    const goneEl = stubElement();
    const remainingEl = stubElement();
    gone.setElement(goneEl);
    remaining.setElement(remainingEl);
    gone.world.set(0, 50, 0);
    remaining.world.set(0, 50, 0);

    declutter(camera(), WIDTH, HEIGHT);
    expect(visible(remainingEl)).toBe(false);

    gone.release();
    declutter(camera(), WIDTH, HEIGHT);
    expect(visible(remainingEl)).toBe(true);
  });

  it('ranks a threatened asset above a quiet one of equal criticality', () => {
    const threatened = label(LabelPriority.ASSET_THREATENED + 10 * 12);
    const quiet = label(LabelPriority.ASSET_BASE + 10 * 12);

    const threatenedEl = stubElement();
    const quietEl = stubElement();
    threatened.setElement(threatenedEl);
    quiet.setElement(quietEl);
    threatened.world.set(0, 50, 0);
    quiet.world.set(0, 50, 0);

    declutter(camera(), WIDTH, HEIGHT);

    expect(visible(threatenedEl)).toBe(true);
    expect(visible(quietEl)).toBe(false);
  });

  it('re-enters the pass after a release and re-attach', () => {
    // Exactly the sequence React Strict Mode produces: mount, clean up, mount again.
    const remounting = acquireLabel(LabelPriority.SELECTED);
    const other = label(LabelPriority.ASSET_BASE + 40);

    const remountingEl = stubElement();
    const otherEl = stubElement();
    remounting.setElement(remountingEl);
    other.setElement(otherEl);
    remounting.world.set(0, 50, 0);
    other.world.set(0, 50, 0);

    remounting.attach();
    remounting.release();
    remounting.attach();

    declutter(camera(), WIDTH, HEIGHT);

    // Still the higher priority, so it still wins — a released-then-reattached handle that
    // silently stopped competing would leave its label undecluttered forever.
    expect(visible(remountingEl)).toBe(true);
    expect(visible(otherEl)).toBe(false);
  });

  it('ranks every track above every asset at rest', () => {
    // The lowest track band against the highest resting asset — 10/10 criticality.
    const lowestTrack = label(LabelPriority.TRACK_LOW);
    const highestAsset = label(LabelPriority.ASSET_BASE + 10 * 12);

    const trackEl = stubElement();
    const assetEl = stubElement();
    lowestTrack.setElement(trackEl);
    highestAsset.setElement(assetEl);
    lowestTrack.world.set(0, 50, 0);
    highestAsset.world.set(0, 50, 0);

    declutter(camera(), WIDTH, HEIGHT);

    expect(visible(trackEl)).toBe(true);
    expect(visible(assetEl)).toBe(false);
  });
});
