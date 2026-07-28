import httpx

from app.db import session_scope
from app.models import ModelRoute


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def test_models_lists_exactly_the_models_with_active_routes(
    client: httpx.AsyncClient, api_key: str, seed_route
) -> None:
    seed_route(model="gpt-4.1-mini", provider="openai", provider_model="gpt-4.1-mini")
    seed_route(
        model="gpt-4.1-mini",
        provider="anthropic",
        provider_model="claude-3-5-haiku-latest",
        position=2,
    )
    seed_route(model="claude-sonnet", provider="anthropic", provider_model="claude-sonnet-4")
    seed_route(model="retired", provider="gemini", provider_model="gemini-1.0")
    with session_scope() as session:
        route = session.query(ModelRoute).filter(ModelRoute.model == "retired").one()
        route.active = False

    response = await client.get("/v1/models", headers=auth(api_key))

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert [item["id"] for item in payload["data"]] == ["claude-sonnet", "gpt-4.1-mini"]
    assert all(item["object"] == "model" for item in payload["data"])
    assert all(item["owned_by"] == "llm-gateway" for item in payload["data"])
    assert all(isinstance(item["created"], int) for item in payload["data"])


async def test_models_requires_a_virtual_key(client: httpx.AsyncClient, db) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_models_is_empty_without_routes(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.get("/v1/models", headers=auth(api_key))
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}
