import "@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "@supabase/supabase-js";

type DevicePayload = {
  device_id?: unknown;
  device_name?: unknown;
  os?: unknown;
  app_version?: unknown;
};

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type, x-nyxsuite-fleet-token",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
};

function jsonResponse(body: Record<string, unknown>, status = 200) {
  return Response.json(body, { status, headers: corsHeaders });
}

function configuredFleetToken() {
  return (Deno.env.get("NYXSUITE_FLEET_TOKEN") || "").trim();
}

function secretKey() {
  const packed = Deno.env.get("SUPABASE_SECRET_KEYS");
  if (packed) {
    try {
      const parsed = JSON.parse(packed);
      const first = parsed.default || Object.values(parsed)[0];
      if (typeof first === "string" && first.trim()) return first.trim();
    } catch (_error) {
      // Fall through to legacy env below.
    }
  }
  return (Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "").trim();
}

function requireFleetToken(req: Request) {
  const expected = configuredFleetToken();
  const actual = (req.headers.get("x-nyxsuite-fleet-token") || "").trim();
  return Boolean(expected && actual && expected === actual);
}

function cleanText(value: unknown, fallback = "", max = 80) {
  const text = String(value ?? "").trim();
  return (text || fallback).slice(0, max);
}

function cleanOs(value: unknown) {
  const raw = cleanText(value, "unknown", 20).toLowerCase();
  if (raw === "windows" || raw === "macos" || raw === "linux") return raw;
  return "unknown";
}

function client() {
  const url = (Deno.env.get("SUPABASE_URL") || "").trim();
  const key = secretKey();
  if (!url || !key) return null;
  return createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

function routePath(req: Request) {
  const parts = new URL(req.url).pathname.split("/").filter(Boolean);
  return parts[parts.length - 1] || "";
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (!requireFleetToken(req)) return jsonResponse({ ok: false, error: "Unauthorized." }, 401);

  const supabase = client();
  if (!supabase) return jsonResponse({ ok: false, error: "Supabase admin client is not configured." }, 500);

  const path = routePath(req);
  if (req.method === "POST" && path === "heartbeat") {
    let payload: DevicePayload = {};
    try {
      payload = await req.json();
    } catch (_error) {
      return jsonResponse({ ok: false, error: "Invalid JSON body." }, 400);
    }

    const deviceId = cleanText(payload.device_id, "", 80);
    if (!deviceId) return jsonResponse({ ok: false, error: "Missing device_id." }, 400);

    const record = {
      device_id: deviceId,
      device_name: cleanText(payload.device_name, "Unnamed device", 80),
      os: cleanOs(payload.os),
      app_version: cleanText(payload.app_version, "", 40),
      last_seen: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };

    const { error } = await supabase
      .from("nyxsuite_device_heartbeats")
      .upsert(record, { onConflict: "device_id" });
    if (error) return jsonResponse({ ok: false, error: error.message }, 500);
    return jsonResponse({ ok: true, message: "Device heartbeat saved." });
  }

  if (req.method === "GET" && path === "devices") {
    const { data, error } = await supabase
      .from("nyxsuite_device_heartbeats")
      .select("device_id,device_name,os,app_version,last_seen")
      .order("last_seen", { ascending: false })
      .limit(250);
    if (error) return jsonResponse({ ok: false, error: error.message }, 500);
    return jsonResponse({ ok: true, devices: data || [] });
  }

  return jsonResponse({ ok: false, error: "Not found." }, 404);
});
