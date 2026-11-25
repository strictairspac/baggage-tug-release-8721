import { request } from "./http.js";

export const api = {
  config: () => request("/workbench"),
  login: data => request("/auth/login", {method: "POST", data}),
  cases: () => request("/cases"),
  createCase: data => request("/cases", {method: "POST", data}),
  runs: () => request("/runs"),
  run: (id, signal) => request(`/runs/${id}`, {signal}),
  enqueue: (id, data) => request(`/cases/${id}/runs`, {method: "POST", data}),
  allocations: params => {
    const query = new URLSearchParams(Object.entries(params || {}).filter(([, value]) => value !== undefined && value !== null && value !== ""));
    const suffix = query.toString() ? `?${query}` : "";
    return request(`/allocations${suffix}`);
  },
  createAllocation: data => request(
    data.case_id ? `/cases/${data.case_id}/allocations` : "/allocations",
    {method: "POST", data},
  ),
  cancelAllocation: id => request(`/allocations/${id}/cancel`, {method: "POST"}),
  cancelAllocationItem: id => request(`/allocations/items/${id}/cancel`, {method: "POST"}),
  review: id => request(`/runs/${id}/review`),
  submitReview: (id, data) => request(`/runs/${id}/review`, {method: "POST", data}),
  export: id => request(`/runs/${id}/export`, {raw: true}),
  audit: () => request("/audit", {signal: AbortSignal.timeout(5000)}),
};
