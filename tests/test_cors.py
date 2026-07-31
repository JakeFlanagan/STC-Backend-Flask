"""Tests for CORS: any localhost/127.0.0.1 origin (any port) should be
allowed, since the dev web frontend and any future local webview frontend
run on a different port than this backend but the same machine."""

from helpers import expect


def test_cors_allows_localhost_origin(client):
    resp = client.get("/api/v1/health", headers={"Origin": "http://localhost:5173"})
    expect("status code", 200, resp.status_code)
    expect("CORS header present", "http://localhost:5173", resp.headers.get("Access-Control-Allow-Origin"))


def test_cors_allows_127_0_0_1_origin(client):
    resp = client.get("/api/v1/health", headers={"Origin": "http://127.0.0.1:8080"})
    expect("status code", 200, resp.status_code)
    expect("CORS header present", "http://127.0.0.1:8080", resp.headers.get("Access-Control-Allow-Origin"))


def test_cors_rejects_non_localhost_origin(client):
    resp = client.get("/api/v1/health", headers={"Origin": "http://evil.example.com"})
    expect("status code", 200, resp.status_code)
    expect("no CORS header for untrusted origin", None, resp.headers.get("Access-Control-Allow-Origin"))
