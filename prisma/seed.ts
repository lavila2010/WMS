import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient();

async function main() {
  const aisleA = await prisma.location.upsert({
    where: { code: "A-01" },
    update: {},
    create: { code: "A-01", name: "Aisle A, Bay 01" },
  });

  const aisleB = await prisma.location.upsert({
    where: { code: "B-04" },
    update: {},
    create: { code: "B-04", name: "Aisle B, Bay 04" },
  });

  await prisma.item.upsert({
    where: { sku: "WIDGET-001" },
    update: {},
    create: {
      sku: "WIDGET-001",
      name: "Standard Widget",
      description: "General purpose widget.",
      quantity: 120,
      locationId: aisleA.id,
    },
  });

  await prisma.item.upsert({
    where: { sku: "GADGET-014" },
    update: {},
    create: {
      sku: "GADGET-014",
      name: "Deluxe Gadget",
      description: "Premium gadget with extended warranty.",
      quantity: 42,
      locationId: aisleB.id,
    },
  });

  console.log("Seed complete.");
}

main()
  .then(async () => {
    await prisma.$disconnect();
  })
  .catch(async (e) => {
    console.error(e);
    await prisma.$disconnect();
    process.exit(1);
  });
