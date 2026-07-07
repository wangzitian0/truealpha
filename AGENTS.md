# TrueAlpha — Agent & Contributor Guide

> **Prohibition**: AI may NOT modify this file without explicit authorization.
> **Language**: All code, PRs, commits, and reports must be in **English**.

---

## 🚨 Security & Red Lines (CRITICAL)

- **NEVER** commit `.env`, `*.pem`, or credential files.
- **NEVER** use float for monetary or exact stock share calculations if precision is required.
- **NEVER** use raw `fetch()` in frontend — use the structured api client wrapper.

---

## 🧱 Structure

- `apps/backend/` — FastAPI + SQLAlchemy + PostgreSQL
- `apps/frontend/` — Next.js + TailwindCSS + ECharts
- `tools/` — Dev scripts
