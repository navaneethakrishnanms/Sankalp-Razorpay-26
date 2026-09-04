const BASE = "/api/bank";

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = "GET", body, token } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* body wasn't JSON */
    }
    throw new ApiError(detail, resp.status);
  }
  return resp.json();
}

export const api = {
  listUsers: () => request("/users"),
  login: (user_id, password) => request("/login", { method: "POST", body: { user_id, password } }),
  session: (token) => request("/session", { token }),
  catalogue: () => request("/catalogue"),
  placeOrder: (token, payload) => request("/orders", { method: "POST", body: payload, token }),
  orderHistory: (token) => request("/orders", { token }),
};

export { ApiError };
