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

function requestToken(req: Request) {
  return (req.headers.get("x-nyxsuite-fleet-token") || "").trim();
}

function requireFleetToken(req: Request) {
  const expected = configuredFleetToken();
  const actual = requestToken(req);
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

async function sha256Hex(value: string) {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

function newDeviceToken() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function readPayload(req: Request): Promise<DevicePayload | null> {
  try {
    const body = await req.json();
    return body && typeof body === "object" ? body as DevicePayload : null;
  } catch (_error) {
    return null;
  }
}

async function hasDeviceCredential(
  supabase: any,
  req: Request,
  deviceId: string,
) {
  const token = requestToken(req);
  if (!token) return false;
  const tokenHash = await sha256Hex(token);
  const { data, error } = await supabase
    .from("nyxsuite_device_credentials")
    .select("token_hash")
    .eq("device_id", deviceId)
    .maybeSingle();
  const credential = data as { token_hash?: string } | null;
  return !error && Boolean(credential?.token_hash && credential.token_hash === tokenHash);
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const supabase = client();
  if (!supabase) return jsonResponse({ ok: false, error: "Supabase admin client is not configured." }, 500);

  const path = routePath(req);
  if (req.method === "POST" && path === "enroll") {
    const payload = await readPayload(req);
    if (!payload) return jsonResponse({ ok: false, error: "Invalid JSON body." }, 400);
    const deviceId = cleanText(payload.device_id, "", 80);
    if (!deviceId) return jsonResponse({ ok: false, error: "Missing device_id." }, 400);

    // Enrollment returns a credential limited to this device's heartbeat. It
    // cannot list the fleet, which remains protected by the owner credential.
    const deviceToken = newDeviceToken();
    const { error } = await supabase
      .from("nyxsuite_device_credentials")
      .upsert({
        device_id: deviceId,
        token_hash: await sha256Hex(deviceToken),
        updated_at: new Date().toISOString(),
      }, { onConflict: "device_id" });
    if (error) return jsonResponse({ ok: false, error: error.message }, 500);
    return jsonResponse({ ok: true, device_token: deviceToken });
  }

  if (req.method === "POST" && path === "heartbeat") {
    const payload = await readPayload(req);
    if (!payload) return jsonResponse({ ok: false, error: "Invalid JSON body." }, 400);

    const deviceId = cleanText(payload.device_id, "", 80);
    if (!deviceId) return jsonResponse({ ok: false, error: "Missing device_id." }, 400);
    if (!requireFleetToken(req) && !await hasDeviceCredential(supabase, req, deviceId)) {
      return jsonResponse({ ok: false, error: "Unauthorized." }, 401);
    }

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
    if (!requireFleetToken(req)) return jsonResponse({ ok: false, error: "Unauthorized." }, 401);
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
