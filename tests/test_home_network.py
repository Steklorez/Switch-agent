import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from switchagent.web.network import home_network_only


@pytest.mark.parametrize("host,headers,status", [
    ("127.0.0.1", {}, 200), ("192.168.1.42", {}, 200),
    ("10.1.2.3", {}, 200), ("8.8.8.8", {}, 403),
    ("8.8.8.8", {"X-Forwarded-For": "127.0.0.1"}, 403),
    ("192.168.1.42", {"Sec-Fetch-Site": "cross-site"}, 403),
])
def test_home_network_access(host, headers, status):
    app = FastAPI()
    app.middleware("http")(home_network_only)
    @app.get("/")
    def index():
        return {"ok": True}
    with TestClient(app, client=(host, 50000)) as client:
        assert client.get("/", headers=headers).status_code == status
