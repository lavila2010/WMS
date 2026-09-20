# WMS V2 UAT Checklist

Bootstrap (do not hardcode passwords):

```
export WMS_UAT_ADMIN_PASSWORD=...
export WMS_UAT_USER_PASSWORD=...
flask --app wsgi create-admin --username leandro
flask --app wsgi create-test-users
```

## Admin

- [ ] Create Client Name + Initials → code `01-XXX`
- [ ] Create ECOM/RTL/WHLS divisions
- [ ] Create NY warehouse; second client also NY
- [ ] Map division↔warehouse; reject cross-client
- [ ] Grant user client access

## Inventory

- [ ] Select Client + Warehouse, upload Excel, preview, confirm
- [ ] Qty 5 creates 5 units
- [ ] Blocking error imports zero
- [ ] Overview ON_HAND = AVAILABLE+RESERVED+PACKED
- [ ] Export Excel

## Orders

- [ ] Select Client + Division, upload multi-line order
- [ ] WMS id `{code}-{number}`
- [ ] Duplicate client order number rejected

## Allocation

- [ ] Reserve same Client/Warehouse/UPC only
- [ ] Partial shortage shown by UPC
- [ ] Two allocators cannot share a unit

## Pick Tickets

- [ ] Only fully ALLOCATED
- [ ] Number `{code}-{orderno}-01`
- [ ] Reprint same number

## Processing

- [ ] Pick ticket lookup, no Client reselect
- [ ] Second user sees: Order is currently being processed by another user.
- [ ] Refresh keeps lock
- [ ] One UPC scan = one unit; excess blocked
- [ ] Recall + reweigh required
- [ ] Close requires ordered=allocated=packed, weights current
- [ ] PDF opens; station returns to Pick Ticket field

## Reports / KPI

- [ ] Closed order on reports + Excel
- [ ] KPI today America/New_York
- [ ] Close moves Open → Processed; Total unchanged
- [ ] Non-admin sees only assigned clients

## Multi-user

- [ ] Two browsers, two orders, both process
- [ ] Same order: exactly one lock winner
