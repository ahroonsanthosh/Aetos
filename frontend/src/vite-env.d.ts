/// <reference types="vite/client" />

/**
 * Vite's `?worker&url` suffix, which its own types do not cover for a path outside the
 * project. Declared here so the bundler-emitted MapLibre worker can be imported by URL with
 * the type checker still able to see what it is — see `components/map/BasemapLayer.tsx` for
 * why the worker has to be requested explicitly at all.
 */
declare module '*?worker&url' {
  const url: string
  export default url
}
