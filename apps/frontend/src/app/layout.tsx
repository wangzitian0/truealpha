import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TrueAlpha Platform",
  description: "DDD Counter Service and Backtesting Scaffolding",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-background text-foreground antialiased min-h-screen flex flex-col">
        {/* Navigation Bar */}
        <header className="border-b border-border bg-card/60 backdrop-blur-md sticky top-0 z-50">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="h-8 w-8 rounded-lg bg-gradient-to-tr from-accent to-violet-400 flex items-center justify-center font-bold text-white shadow-lg shadow-accent/20">
                α
              </div>
              <span className="text-xl font-bold bg-gradient-to-r from-white to-gray-400 bg-clip-text text-transparent tracking-tight">
                TrueAlpha
              </span>
            </div>
            <nav className="flex items-center gap-6">
              <a href="/" className="text-sm font-medium text-foreground hover:text-accent transition-colors">
                Dashboard
              </a>
              <a href="/docs" className="text-sm font-medium text-gray-400 hover:text-foreground transition-colors">
                Docs
              </a>
            </nav>
          </div>
        </header>

        {/* Main Content Area */}
        <main className="flex-grow max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 w-full">
          {children}
        </main>

        {/* Footer */}
        <footer className="border-t border-border bg-card/20 py-6 text-center text-sm text-gray-500">
          <p>© {new Date().getFullYear()} TrueAlpha. Built with DDD Bounded Contexts.</p>
        </footer>
      </body>
    </html>
  );
}
