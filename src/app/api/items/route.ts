import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";

export const dynamic = "force-dynamic";

export async function GET() {
  const items = await prisma.item.findMany({
    orderBy: { createdAt: "desc" },
    include: { location: true },
  });
  return NextResponse.json({ items });
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => null);
  if (!body || typeof body.sku !== "string" || typeof body.name !== "string") {
    return NextResponse.json(
      { error: "sku and name are required" },
      { status: 400 },
    );
  }

  try {
    const item = await prisma.item.create({
      data: {
        sku: body.sku,
        name: body.name,
        description:
          typeof body.description === "string" ? body.description : null,
        quantity: Number.isFinite(body.quantity) ? Number(body.quantity) : 0,
        locationId:
          typeof body.locationId === "string" && body.locationId
            ? body.locationId
            : null,
      },
    });
    return NextResponse.json({ item }, { status: 201 });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 409 });
  }
}
