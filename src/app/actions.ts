"use server";

import { revalidatePath } from "next/cache";
import { prisma } from "@/lib/prisma";

export async function createItem(formData: FormData) {
  const sku = String(formData.get("sku") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  const description = String(formData.get("description") ?? "").trim();
  const quantity = Number(formData.get("quantity") ?? 0);
  const locationId = String(formData.get("locationId") ?? "").trim();

  if (!sku || !name) {
    throw new Error("SKU and name are required.");
  }

  await prisma.item.create({
    data: {
      sku,
      name,
      description: description || null,
      quantity: Number.isFinite(quantity) ? quantity : 0,
      locationId: locationId || null,
    },
  });

  revalidatePath("/");
}
