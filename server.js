const http = require('http');
const url = require('url');

// In-memory stores for vendors and products
const vendors = [];
const products = [];
let nextVendorId = 1;
let nextProductId = 1;

function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      if (body.length > 1e6) {
        req.connection.destroy();
        reject(new Error('Body too large'));
      }
    });
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (err) {
        reject(err);
      }
    });
  });
}

function sendJson(res, status, data) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
}

const server = http.createServer(async (req, res) => {
  const parsed = url.parse(req.url, true);
  const { pathname } = parsed;

  // Vendor endpoints
  if (pathname === '/vendors' && req.method === 'GET') {
    return sendJson(res, 200, vendors);
  }
  if (pathname === '/vendors' && req.method === 'POST') {
    try {
      const body = await parseBody(req);
      if (!body.name) {
        return sendJson(res, 400, { error: 'name is required' });
      }
      const vendor = { id: nextVendorId++, name: body.name };
      vendors.push(vendor);
      return sendJson(res, 201, vendor);
    } catch (err) {
      return sendJson(res, 400, { error: 'Invalid JSON' });
    }
  }

  // Product endpoints
  if (pathname === '/products' && req.method === 'GET') {
    const { vendorId } = parsed.query;
    if (vendorId) {
      const filtered = products.filter(p => p.vendorId === parseInt(vendorId, 10));
      return sendJson(res, 200, filtered);
    }
    return sendJson(res, 200, products);
  }
  if (pathname === '/products' && req.method === 'POST') {
    try {
      const body = await parseBody(req);
      const { name, price, vendorId } = body;
      if (!name || price == null || !vendorId) {
        return sendJson(res, 400, { error: 'name, price, and vendorId are required' });
      }
      const vendorExists = vendors.some(v => v.id === vendorId);
      if (!vendorExists) {
        return sendJson(res, 404, { error: 'Vendor not found' });
      }
      const product = { id: nextProductId++, name, price, vendorId };
      products.push(product);
      return sendJson(res, 201, product);
    } catch (err) {
      return sendJson(res, 400, { error: 'Invalid JSON' });
    }
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Not Found' }));
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});

