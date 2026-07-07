import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        background: "#0d0e12",
        foreground: "#f3f4f6",
        card: "#15171e",
        border: "#23262f",
        accent: {
          DEFAULT: "#4f46e5",
          hover: "#6366f1",
          muted: "#312e81",
        },
      },
    },
  },
  plugins: [],
};

export default config;
