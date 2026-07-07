export function shouldProxyApiToLocalBackend() {
    return process.env.NODE_ENV !== 'production' && (process.env.NEXT_PUBLIC_API_URL ?? '') === '';
}

/** @type {import('next').NextConfig} */
const nextConfig = {
    async rewrites() {
        if (!shouldProxyApiToLocalBackend()) return [];
        return [{ source: '/api/:path*', destination: 'http://localhost:8000/api/:path*' }];
    },
};

export default nextConfig;
