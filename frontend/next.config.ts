import type { NextConfig } from "next";

const defaultCloudUrl = "https://harder-heads-procedure-modems.trycloudflare.com";
const backendUrl =
  process.env.BACKEND_URL ||
  (process.env.VERCEL ? defaultCloudUrl : "http://127.0.0.1:5286");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: "/backend/:path*",
        destination: `${backendUrl}/:path*`,
      },
    ];
  },
};

export default nextConfig;
