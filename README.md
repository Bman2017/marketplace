# Marketplace

This repository contains a minimal Node.js prototype for a multivendor marketplace API using only built-in modules.

## Running

```bash
npm start
```

The server listens on port **3000** by default.

## API Endpoints

### Vendors
- `GET /vendors` — list all vendors.
- `POST /vendors` — create a vendor with JSON body `{ "name": "Acme" }`.

### Products
- `GET /products` — list all products. Filter by vendor with `?vendorId=1`.
- `POST /products` — create a product with JSON body `{ "name": "Widget", "price": 9.99, "vendorId": 1 }`.

This is an in-memory implementation meant as a starting point; it does not persist data or include authentication.
