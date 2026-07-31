"""Tests for /api/templates* routes (blueprints/templates_bp.py): saved
listing description snippets."""

from helpers import expect


def test_create_template(client):
    resp = client.post("/api/v1/templates", json={"category": "Retro Gaming", "text": "Tested working, ships in 24h."})
    expect("status code", 201, resp.status_code)
    body = resp.get_json()
    expect("category", "Retro Gaming", body["category"])
    expect("text", "Tested working, ships in 24h.", body["text"])


def test_create_template_rejects_missing_fields(client):
    resp = client.post("/api/v1/templates", json={"category": "", "text": ""})
    expect("status code", 400, resp.status_code)


def test_list_templates_sorted_by_category(client):
    client.post("/api/v1/templates", json={"category": "Zebra", "text": "z"})
    client.post("/api/v1/templates", json={"category": "Apple", "text": "a"})

    resp = client.get("/api/v1/templates")
    expect("status code", 200, resp.status_code)
    categories = [t["category"] for t in resp.get_json()]
    expect("sorted alphabetically by category", ["Apple", "Zebra"], categories)


def test_update_template(client):
    created = client.post("/api/v1/templates", json={"category": "Cat", "text": "old text"}).get_json()
    resp = client.put(f"/api/v1/templates/{created['id']}", json={"category": "New Cat", "text": "new text", "version": created["version"]})
    expect("status code", 200, resp.status_code)
    body = resp.get_json()
    expect("category updated", "New Cat", body["category"])
    expect("text updated", "new text", body["text"])
    expect("id unchanged", created["id"], body["id"])

    confirm = client.get("/api/v1/templates").get_json()
    expect("only one template exists (edited, not duplicated)", 1, len(confirm))


def test_update_template_rejects_missing_fields(client):
    created = client.post("/api/v1/templates", json={"category": "Cat", "text": "text"}).get_json()
    resp = client.put(f"/api/v1/templates/{created['id']}", json={"category": "", "text": "", "version": created["version"]})
    expect("status code", 400, resp.status_code)


def test_update_template_rejects_stale_version(client):
    created = client.post("/api/v1/templates", json={"category": "Cat", "text": "v1 text"}).get_json()
    expect("initial version", 1, created["version"])

    first_put = client.put(f"/api/v1/templates/{created['id']}", json={"category": "Cat", "text": "v2 text", "version": 1})
    expect("first update succeeds", 200, first_put.status_code)
    expect("version bumped to 2", 2, first_put.get_json()["version"])

    stale_put = client.put(f"/api/v1/templates/{created['id']}", json={"category": "Cat", "text": "v3 text", "version": 1})
    expect("stale write rejected", 409, stale_put.status_code)
    body = stale_put.get_json()
    expect("error code", "version_conflict", body["error"])
    expect("current text reflects the winning edit", "v2 text", body["current"]["text"])


def test_update_template_requires_version(client):
    created = client.post("/api/v1/templates", json={"category": "Cat", "text": "text"}).get_json()
    resp = client.put(f"/api/v1/templates/{created['id']}", json={"category": "Cat", "text": "new text"})
    expect("status code", 400, resp.status_code)
    expect("error", "version is required", resp.get_json()["error"])


def test_update_template_404_for_missing_id(client):
    resp = client.put("/api/v1/templates/999999", json={"category": "Cat", "text": "text"})
    expect("status code", 404, resp.status_code)


def test_delete_template(client):
    created = client.post("/api/v1/templates", json={"category": "Cat", "text": "text"}).get_json()
    resp = client.delete(f"/api/v1/templates/{created['id']}")
    expect("status code", 200, resp.status_code)

    remaining = [t["id"] for t in client.get("/api/v1/templates").get_json()]
    expect("template no longer listed", False, created["id"] in remaining)
