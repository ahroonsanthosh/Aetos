/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // three ships untranspiled ESM examples; Next handles it, but keep the package
  // boundary explicit so a future monorepo move does not silently change behaviour.
  transpilePackages: ['three'],
};

export default nextConfig;
