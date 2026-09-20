# WMS

A minimal Warehouse Management System (WMS) starter built with **Next.js (App Router, TypeScript)**, **Prisma**, and **PostgreSQL**.

It tracks inventory items (SKU, name, quantity, location) and warehouse locations, with a web UI and a small JSON API.

## Stack

- [Next.js 15](https://nextjs.org/) (App Router, React 19, server actions)
- [Prisma 6](https://www.prisma.io/) ORM
- [PostgreSQL 16](https://www.postgresql.org/)
- TypeScript

## Prerequisites

- Node.js 22+
- pnpm 10+
- A running PostgreSQL instance

## Setup

1. Install dependencies:

   ```bash
   pnpm install
   ```

2. Configure the database connection. Copy `.env.example` to `.env` and adjust `DATABASE_URL`:

   ```bash
   cp .env.example .env
   ```

3. Generate the Prisma client and apply migrations:

   ```bash
   pnpm prisma:generate
   pnpm prisma:migrate:dev
   ```

4. (Optional) Seed sample data:

   ```bash
   pnpm db:seed
   ```

5. Start the dev server:

   ```bash
   pnpm dev
   ```

   The app runs at [http://localhost:3000](http://localhost:3000).

## Scripts

| Script                 | Description                                  |
| ---------------------- | -------------------------------------------- |
| `pnpm dev`             | Start the Next.js dev server                 |
| `pnpm build`           | Production build                             |
| `pnpm start`           | Start the production server                  |
| `pnpm lint`            | Run ESLint                                   |
| `pnpm typecheck`       | Type-check with `tsc --noEmit`               |
| `pnpm prisma:generate` | Generate the Prisma client                   |
| `pnpm prisma:migrate`  | Apply migrations (`migrate deploy`)          |
| `pnpm db:seed`         | Seed sample locations and items              |

## API

- `GET /api/health` — liveness + database connectivity check
- `GET /api/items` — list inventory items
- `POST /api/items` — create an item (`{ "sku", "name", "quantity?", "description?", "locationId?" }`)
