from __future__ import annotations


def test_index_lists_human_entry_points(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {
        "service": "dtcc-upload",
        "status": "ok",
        "links": {
            "docs": "/docs",
            "health": "/healthz",
            "ready": "/readyz",
            "datasets": "/v1/datasets",
        },
    }


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
