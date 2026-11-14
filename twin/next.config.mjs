/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // three ships untranspiled ESM examples; Next handles it, but keep the package
  // boundary explicit so a future monorepo move does not silently change behaviour.
  transpilePackages: ['three'],
  // Built to static files and served by the Aetos backend at /twin, next to the console, so
  // the twin is part of the same site rather than a second server to run and host.
  output: 'export',
  basePath: '/twin',
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
