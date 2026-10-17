/** @type {import('next').NextConfig} */
const nextConfig = {
  eslint: {
    ignoreDuringBuilds: true,
  },
  // The container runs tsc explicitly before next build; avoid Next's duplicate
  // worker check there. Local builds still use Next's normal type check.
  typescript: {
    ignoreBuildErrors: process.env.NEXT_IGNORE_BUILD_TYPE_ERRORS === 'true',
  },
  images: { unoptimized: true },
  experimental: {
    cpus: 1,
    workerThreads: false,
  },
};

module.exports = nextConfig;
