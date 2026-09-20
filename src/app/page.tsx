import { prisma } from "@/lib/prisma";
import { createItem } from "./actions";

export const dynamic = "force-dynamic";

export default async function Home() {
  const [items, locations] = await Promise.all([
    prisma.item.findMany({
      orderBy: { createdAt: "desc" },
      include: { location: true },
    }),
    prisma.location.findMany({ orderBy: { code: "asc" } }),
  ]);

  const totalUnits = items.reduce((sum, item) => sum + item.quantity, 0);

  return (
    <main className="container">
      <div className="header">
        <h1>WMS — Warehouse Inventory</h1>
        <span className="tag">
          {items.length} SKUs · {totalUnits} units on hand
        </span>
      </div>

      <section className="card">
        <h2>Add inventory item</h2>
        <form action={createItem}>
          <div className="grid">
            <label>
              SKU
              <input name="sku" placeholder="WIDGET-002" required />
            </label>
            <label>
              Name
              <input name="name" placeholder="Standard Widget" required />
            </label>
            <label>
              Quantity
              <input name="quantity" type="number" min="0" defaultValue={0} />
            </label>
            <label>
              Location
              <select name="locationId" defaultValue="">
                <option value="">— Unassigned —</option>
                {locations.map((loc) => (
                  <option key={loc.id} value={loc.id}>
                    {loc.code} — {loc.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label style={{ marginTop: "1rem" }}>
            Description
            <input name="description" placeholder="Optional notes" />
          </label>
          <button type="submit">Add item</button>
        </form>
      </section>

      <section className="card">
        <h2>Inventory</h2>
        {items.length === 0 ? (
          <p className="empty">No items yet. Add one above to get started.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>SKU</th>
                <th>Name</th>
                <th>Location</th>
                <th>Description</th>
                <th style={{ textAlign: "right" }}>Qty</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td className="sku">{item.sku}</td>
                  <td>{item.name}</td>
                  <td>{item.location ? item.location.code : "—"}</td>
                  <td>{item.description ?? "—"}</td>
                  <td className="qty" style={{ textAlign: "right" }}>
                    {item.quantity}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
