import type { Metadata } from "next";
import { AppChrome } from "@/components/app-chrome";
import "./globals.css";

/* #371: the top bar renders the verified session's identity, so NO route may
 * be prerendered. Two reasons, and the second is the load-bearing one:
 *   1. `/` is a bare redirect and was static, so a production build tried to
 *      resolve auth config at build time and failed — `SECRET_KEY` does not
 *      exist in the image build, and giving it one would be papering over (2).
 *   2. A prerendered layout that renders identity bakes ONE visitor's top bar
 *      into the static output every later request would then be served.
 * Every page under /research and /admin is already force-dynamic; this makes
 * the property hold at the root, where the chrome actually lives. */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "TrueAlpha",
  description: "Personal fundamental & supply-chain investment research",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-background text-foreground antialiased min-h-screen flex flex-col">
        <header className="border-b border-border bg-card/60 backdrop-blur-md sticky top-0 z-50">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center gap-3">
            <div className="h-8 w-8 rounded-lg bg-gradient-to-tr from-accent to-violet-400 flex items-center justify-center font-bold text-white shadow-lg shadow-accent/20">
              α
            </div>
            <span className="text-xl font-bold bg-gradient-to-r from-white to-gray-400 bg-clip-text text-transparent tracking-tight">
              TrueAlpha
            </span>
            {/* #371: who is signed in, and the way into the other world. Both
                derived from the verified session inside AppChrome, never from
                anything the client supplies. */}
            <AppChrome />
          </div>
        </header>
        <main className="flex-grow max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 w-full">
          {children}
        </main>
      </body>
    </html>
  );
}
